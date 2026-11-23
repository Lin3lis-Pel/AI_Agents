"""The things the graph depends on, gathered in one object.

Nodes never create their own LLM client, database connection or HTTP client. They get
them from `Deps`. That is what makes the graph testable: a test passes a fake chat model
and a temporary database, and every node and tool in between is the real one.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from agent.config import Settings
from agent.llm import LLMDeadlineError, UsageRecord, call_json, get_chat_model
from agent.state import ReviewState
from memory.store import MemoryStore
from rag.retriever import search_gcp_docs
from tools.billing_api import BillingCatalogClient, load_prices
from tools.models import Doc
from tools.pricing import PriceTable

SearchFn = Callable[[str, str | None, int], list[Doc]]
PROMPTS_DIR = Path(__file__).parent / "prompts"
LIVE_PRICES_S = 24 * 3600  # list prices change rarely
FALLBACK_PRICES_S = 300  # after a failure, try the API again before long
T = TypeVar("T", bound=BaseModel)


@dataclass
class Deps:
    settings: Settings
    llm: BaseChatModel | None
    store: MemoryStore
    search: SearchFn
    billing_client: BillingCatalogClient | None = None
    clock: Callable[[], float] = time.perf_counter
    _prices: tuple[float, PriceTable] | None = field(default=None, repr=False)

    def prices(self) -> PriceTable:
        """The price table, kept for a day (the Billing API call is slow), or for minutes
        when it is a fallback: one bad answer must not decide prices until a restart."""
        if self._prices is None or self.clock() >= self._prices[0]:
            prices = load_prices(self.billing_client)
            failed = self.billing_client is not None and prices.fallback_reason is not None
            keep_s = FALLBACK_PRICES_S if failed else LIVE_PRICES_S
            self._prices = (self.clock() + keep_s, prices)
        return self._prices[1]

    def ask(
        self, state: ReviewState, node: str, system: str, user: str, schema: type[T]
    ) -> tuple[T, list[UsageRecord]]:
        """One LLM step of a review, inside the step's and the review's time limit."""
        limit = self.settings.llm_step_deadline_s
        if state.deadline_at is not None:
            limit = min(limit, state.deadline_at - self.clock())
        if limit <= 0 and self.llm is not None:
            raise LLMDeadlineError("The review's time limit was reached.")
        return call_json(self.llm, node, system, user, schema, timeout_s=limit)

    def prompt(self, name: str) -> str:
        """Load a prompt template from `agent/prompts/<name>.md`."""
        if name == "write_report" and self.settings.report_prompt_path is not None:
            return self.settings.report_prompt_path.read_text(encoding="utf-8")
        return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def default_deps(settings: Settings | None = None) -> Deps:
    """Production wiring: real LLM (if a key is set), real store, real retriever."""
    settings = settings or Settings.from_env()
    billing_key = os.getenv("GCP_BILLING_API_KEY", "").strip()
    return Deps(
        settings=settings,
        llm=get_chat_model(settings),
        store=MemoryStore(settings.memory_path),
        search=lambda query, service, k: search_gcp_docs(
            query, service, k, store_dir=settings.chroma_dir
        ),
        billing_client=BillingCatalogClient(billing_key) if billing_key else None,
    )
