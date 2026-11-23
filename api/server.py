"""HTTP API for the Node.js UI. A thin adapter over `agent.service.ReviewService`.

    uv run python -m api.server        # serves http://127.0.0.1:8000

Every route translates a request into one service call and the result into JSON and a
status code. Nothing is decided here. The server listens on localhost only: it has no
authentication and is meant to sit behind the Node.js UI on the same machine.

Routes:
    GET  /api/capabilities                  what the tool supports
    GET  /api/samples                       example Terraform files
    GET  /api/samples/{name}                the text of one example
    GET  /api/projects/{project}/memory     a project's budget, region and preferences
    PUT  /api/projects/{project}/memory
    POST /api/reviews                       run a review; streams one JSON event per line
    POST /api/reviews/{review_id}/feedback  finish a review with the user's feedback
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.service import (
    Capabilities,
    FeedbackChoice,
    FinishResult,
    ReviewEvent,
    ReviewOptions,
    ReviewService,
)
from api.request_log import RequestLog
from memory.store import ProjectConstraints
from tools.logs import configure_logging, log_event

HOST, PORT = "127.0.0.1", 8000
MAX_BODY_CHARS = 1_000_000  # far above the 200 KB input limit; stops absurd requests early
PROJECT_ID = r"^[A-Za-z0-9_.-]{1,64}$"
ProjectId = Annotated[str, Path(pattern=PROJECT_ID)]
FINISH_STATUS = {"ok": 200, "not_found": 404, "already_finished": 409}


class ReviewRequest(BaseModel):
    terraform: str = Field(max_length=MAX_BODY_CHARS)
    project_id: str = Field(default="default", pattern=PROJECT_ID)
    options: ReviewOptions | None = None


class SampleList(BaseModel):
    samples: list[str]


class SampleFile(BaseModel):
    name: str
    terraform: str


class NdjsonResponse(StreamingResponse):
    media_type = "application/x-ndjson"


class FeedbackRequest(BaseModel):
    feedback: list[FeedbackChoice] = Field(default_factory=list, max_length=50)


def create_app(service: ReviewService | None = None) -> FastAPI:
    service = service or ReviewService()
    app = FastAPI(
        title="GCP Infra Review Agent API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.service = service
    app.add_middleware(RequestLog)

    @app.get("/api/capabilities")
    def capabilities() -> Capabilities:
        return service.capabilities()

    @app.get("/api/samples")
    def samples() -> SampleList:
        return SampleList(samples=service.samples())

    @app.get("/api/samples/{name:path}")
    def sample(name: str) -> SampleFile:
        terraform = service.sample(name)
        if terraform is None:
            raise HTTPException(status_code=404, detail="No such sample.")
        return SampleFile(name=name, terraform=terraform)

    @app.get("/api/projects/{project_id}/memory")
    def get_memory(project_id: ProjectId) -> ProjectConstraints:
        return service.get_memory(project_id)

    @app.put("/api/projects/{project_id}/memory")
    def save_memory(project_id: ProjectId, constraints: ProjectConstraints) -> ProjectConstraints:
        service.save_memory(project_id, constraints)
        return service.get_memory(project_id)

    # The response is a stream of `ReviewEvent`, one JSON object per line, sent as each
    # step finishes. `responses` puts the events into the OpenAPI schema, from which the
    # UI's TypeScript types are generated (api/schema.py).
    @app.post(
        "/api/reviews",
        response_class=NdjsonResponse,
        responses={200: {"model": ReviewEvent, "description": "One event per line"}},
    )
    def review(body: ReviewRequest, request: Request) -> NdjsonResponse:
        def lines() -> Iterator[str]:
            for event in service.stream_review(
                body.terraform, body.project_id, body.options, request.state.request_id
            ):
                yield event.model_dump_json() + "\n"

        return NdjsonResponse(lines())

    @app.post("/api/reviews/{review_id}/feedback")
    def feedback(review_id: str, request: FeedbackRequest) -> FinishResult:
        result = service.finish_review(review_id, request.feedback)
        if result.status != "ok":
            raise HTTPException(status_code=FINISH_STATUS[result.status], detail=result.status)
        return result

    return app


if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    configure_logging()
    log_event("server.started", host=HOST, port=PORT)
    uvicorn.run(create_app(), host=HOST, port=PORT)
