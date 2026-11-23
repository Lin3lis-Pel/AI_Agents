"""The review service: what any front end can do with the agent, in one place.

The HTTP API (`api/`) and the MCP server are thin adapters over this module. They
translate requests and responses; they do not decide anything. A front end never touches
the graph, the store or the settings directly.

Reviews run through `ReviewSession`, so the security guarantees are the ones of the
graph: nothing here can add, remove or change a security finding.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agent.config import Settings
from agent.deps import Deps, default_deps
from agent.runner import ReviewSession, new_review_id
from agent.state import ReviewReport
from memory.feedback_policy import PROTECTED_CATEGORIES
from memory.store import FeedbackAction, FeedbackItem, ProjectConstraints
from tools.logs import log_event, log_failure
from tools.models import Service
from tools.security_rules import RULES
from tools.tf_parser import MAX_INPUT_BYTES, SUPPORTED_TYPES, TerraformInputError

DepsFactory = Callable[[Settings], Deps]
MAX_OPEN_REVIEWS = 200  # reviews kept in memory so their feedback can still be sent


class Capabilities(BaseModel):
    """Facts about what the tool supports. The wording around them belongs to the UI."""

    services: list[str]
    resource_types: list[str]
    rule_count: int
    max_input_kb: int
    llm_configured: bool
    model: str


class ReviewOptions(BaseModel):
    """Advanced settings for one review. Anything left out keeps the configured default.

    None of these can change a security finding: the rules do not read them.
    """

    model: str | None = Field(default=None, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=1)
    use_rag: bool | None = None
    max_rewrites: int | None = Field(default=None, ge=0, le=2)
    extra_instructions: str | None = Field(default=None, max_length=1000)


class StepEvent(BaseModel):
    """A graph node just finished."""

    type: Literal["step"] = "step"
    step: str


class ReportEvent(BaseModel):
    """The review is done. Always the last event."""

    type: Literal["report"] = "report"
    report: ReviewReport
    waiting_for_feedback: bool


class ProblemEvent(BaseModel):
    """The input was rejected, or the review failed. Always the last event."""

    type: Literal["rejected", "error"]
    message: str  # safe to show to a user
    review_id: str  # the review's id in the server log


# What a review sends, one event at a time. A union rather than one class with optional
# fields, so that the schema says which fields each event really has.
ReviewEvent = Annotated[StepEvent | ReportEvent | ProblemEvent, Field(discriminator="type")]


class FeedbackChoice(BaseModel):
    """The user's answer about one finding, named by its position in the report.

    The front end sends a position, not a key, so it cannot make up a finding to dismiss.
    """

    finding_index: int = Field(ge=0)
    action: FeedbackAction
    comment: str = Field(default="", max_length=300)


class FinishResult(BaseModel):
    status: Literal["ok", "not_found", "already_finished"]
    saved: int = 0
    ignored: int = 0  # answers about security findings or positions that do not exist


def _outcome(report: ReviewReport) -> dict[str, object]:
    """What the log says about a finished review: counts and times, no content."""
    return {
        "status": report.status,
        "model": report.model,
        "findings": len(report.findings),
        "warnings": len(report.warnings),
        "llm_calls": report.usage.llm_calls,
        "cost_usd": report.usage.cost_usd,
        "seconds": report.timings.get("total"),
    }


class ReviewService:
    def __init__(
        self, deps_factory: DepsFactory = default_deps, settings: Settings | None = None
    ) -> None:
        self._deps_factory = deps_factory
        self._settings = settings or Settings.from_env()
        self._sessions: dict[str, ReviewSession] = {}
        self._reviews: dict[str, tuple[ReviewSession, ReviewReport]] = {}
        self._default = self._session(self._settings)

    def _session(self, settings: Settings) -> ReviewSession:
        """One session per distinct settings: building the graph and clients is not free."""
        key = settings.model_dump_json()
        if key not in self._sessions:
            self._sessions[key] = ReviewSession(self._deps_factory(settings))
        return self._sessions[key]

    def capabilities(self) -> Capabilities:
        return Capabilities(
            services=[service.value for service in Service],
            resource_types=sorted(SUPPORTED_TYPES),
            rule_count=len(RULES),
            max_input_kb=MAX_INPUT_BYTES // 1000,
            llm_configured=self._default.deps.llm is not None,
            model=self._settings.model,
        )

    def stream_review(
        self,
        terraform: str,
        project_id: str = "default",
        options: ReviewOptions | None = None,
        request_id: str | None = None,
    ) -> Iterator[ReviewEvent]:
        """Review `terraform`, yielding one event per finished step and the outcome last.

        Expected problems are events, not exceptions: a front end shows `message` as is.
        The cause of an unexpected one is in the log, under the review id in the event.
        """
        ids = {"review_id": new_review_id(), "request_id": request_id}
        log_event("review.started", **ids)
        try:
            overrides = options.model_dump(exclude_none=True) if options else {}
            session = self._session(self._settings.model_copy(update=overrides))
            for item in session.start_steps(terraform, project_id, ids["review_id"]):
                if isinstance(item, ReviewReport):
                    self._remember(session, item)
                    log_event("review.finished", **ids, **_outcome(item))
                    yield ReportEvent(
                        report=item, waiting_for_feedback=session.is_waiting(item.review_id)
                    )
                else:
                    yield StepEvent(step=item)
        except TerraformInputError as exc:
            log_event("review.rejected", **ids, reason=type(exc).__name__)
            yield ProblemEvent(type="rejected", message=str(exc), review_id=ids["review_id"])
        except Exception as exc:  # a clear message, never a stack trace
            log_failure("review.failed", exc, **ids)
            yield ProblemEvent(
                type="error",
                message=f"The review failed unexpectedly ({type(exc).__name__}). "
                f"Please try again. Reference: {ids['review_id']}.",
                review_id=ids["review_id"],
            )

    def _remember(self, session: ReviewSession, report: ReviewReport) -> None:
        self._reviews[report.review_id] = (session, report)
        while len(self._reviews) > MAX_OPEN_REVIEWS:
            self._reviews.pop(next(iter(self._reviews)))  # the oldest one

    def finish_review(self, review_id: str, feedback: list[FeedbackChoice]) -> FinishResult:
        """Send the user's feedback for a review that is waiting for it, and save it.

        Answers about security findings are ignored here as well as by the feedback
        policy: feedback may only lower the priority of cost, sizing and simplification
        suggestions.
        """
        if review_id not in self._reviews:
            return FinishResult(status="not_found")
        session, report = self._reviews[review_id]
        if not session.is_waiting(review_id):
            return FinishResult(status="already_finished")
        items = []
        for choice in feedback:
            if choice.finding_index >= len(report.findings):
                continue
            finding = report.findings[choice.finding_index]
            if finding.category in PROTECTED_CATEGORIES:
                continue
            items.append(
                FeedbackItem(
                    finding_key=finding.feedback_key,
                    category=finding.category.value,
                    action=choice.action,
                    comment=choice.comment,
                )
            )
        session.finish(review_id, items)
        return FinishResult(status="ok", saved=len(items), ignored=len(feedback) - len(items))

    def get_memory(self, project_id: str) -> ProjectConstraints:
        return self._default.deps.store.get_constraints(project_id)

    def save_memory(self, project_id: str, constraints: ProjectConstraints) -> None:
        self._default.deps.store.save_constraints(project_id, constraints)

    def samples(self) -> list[str]:
        """Names of the example Terraform files a user can start from."""
        from evals.dataset_loader import DATASET_DIR

        names = (p.relative_to(DATASET_DIR).as_posix() for p in DATASET_DIR.rglob("*.tf"))
        return sorted(name for name in names if not name.startswith("heldout/"))

    def sample(self, name: str) -> str | None:
        """The text of one example file, or None. Only names from `samples()` are read."""
        from evals.dataset_loader import DATASET_DIR, read_terraform

        return read_terraform(DATASET_DIR / name) if name in self.samples() else None
