"""The real HTTP API with a scripted LLM, for the browser tests in `web/e2e/`.

    uv run python -m tests.e2e_server

Playwright starts it. The graph, the rules, the cost estimate, the document store and
the SQLite memory are the real ones; the memory is a new temporary file each time. Only
the LLM is faked, so the browser tests need no key, cost nothing and always see the same
report. It uses the port of the real API, so it cannot run next to it by accident.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GCP_BILLING_API_KEY", "LANGSMITH_API_KEY"):
    os.environ[name] = ""  # before anything loads .env: no test may reach a paid service
os.environ["LANGSMITH_TRACING"] = "false"

import uvicorn  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.deps import Deps  # noqa: E402
from agent.service import ReviewService  # noqa: E402
from api.server import HOST, PORT, create_app  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from rag.ingest import build_store  # noqa: E402
from rag.retriever import search_gcp_docs  # noqa: E402
from tests.conftest import FakeChatModel  # noqa: E402
from tools.logs import configure_logging  # noqa: E402

RESOURCE = "google_sql_database_instance.main"
HA_DOC = "cloud_sql/high-availability"
WRITING_S = 2.0  # long enough for the browser to see the steps before the report


def _documents(prompt: str) -> list[dict]:
    payload = prompt.split("<review_data>")[1].split("</review_data>")[0]
    return json.loads(payload)["documents"]


def _keep(prompt: str) -> dict:
    ids = [doc["id"] for doc in _documents(prompt)]
    return {"relevant_ids": [doc_id for doc_id in ids if doc_id.startswith(HA_DOC)]}


def _write(prompt: str) -> dict:
    time.sleep(WRITING_S)
    doc_id = next(d["id"] for d in _documents(prompt) if d["id"].startswith(HA_DOC))
    return {
        "summary": "One highly available database that is open to the internet.",
        "recommendations": [
            {
                "resource": RESOURCE,
                "category": "cost",
                "severity": "medium",
                "issue": "High availability doubles the cost of this instance.",
                "recommendation": "Use ZONAL availability if this is not production.",
                "estimated_savings_usd": 115.62,
                "doc_id": doc_id,
            }
        ],
    }


def scripted_llm() -> FakeChatModel:
    query = {"query": "cost of a regional standby instance", "service": "cloud_sql"}
    return FakeChatModel(
        replies={
            "PLAN_RESEARCH": {"queries": [query]},
            "GRADE_DOCS": _keep,
            "WRITE_REPORT": _write,
        },
        calls=[],
    )


def build_service(data_dir: Path) -> ReviewService:
    store_dir = data_dir / "chroma"
    build_store(store_dir)
    memory = MemoryStore(data_dir / "memory.sqlite")
    llm = scripted_llm()

    def deps(settings: Settings) -> Deps:
        return Deps(
            settings=settings,
            llm=llm,
            store=memory,
            search=lambda query, service, k: search_gcp_docs(
                query, service, k, store_dir=store_dir
            ),
        )

    return ReviewService(deps, Settings(data_dir=data_dir))


if __name__ == "__main__":
    configure_logging()
    service = build_service(Path(tempfile.mkdtemp(prefix="gcp-review-e2e-")))
    uvicorn.run(create_app(service), host=HOST, port=PORT)
