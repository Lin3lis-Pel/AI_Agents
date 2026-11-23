"""Logs must explain a failure without keeping a secret or the user's Terraform."""

import json
import logging

import pytest
from pydantic import BaseModel

from tools.logs import log_event, log_failure, review_context

SECRET = "super-secret-value-123"


def test_each_event_is_one_json_line_with_its_review_id(log_lines):
    with review_context("abc123"):
        log_event("review.step", node="parse", seconds=0.1)
    log_event("server.started", port=8000)

    first, second = log_lines()
    assert first["event"] == "review.step"
    assert (first["review_id"], first["node"]) == ("abc123", "parse")
    assert "review_id" not in second


def test_a_failure_is_logged_with_the_cause_behind_it_and_where_it_happened(log_lines):
    try:
        try:
            raise ConnectionResetError("peer closed the connection")
        except ConnectionResetError as cause:
            raise RuntimeError("the call failed") from cause
    except RuntimeError as exc:
        log_failure("llm.failed", exc, node="write_report")

    (line,) = log_lines()
    assert [c["type"] for c in line["causes"]] == ["RuntimeError", "ConnectionResetError"]
    assert line["causes"][1]["message"] == "peer closed the connection"
    assert "test_logs.py" in line["causes"][0]["at"][-1]


@pytest.mark.parametrize(
    "message",
    [
        f"database password is {SECRET}",
        f"GET https://example.com/skus?key={SECRET}&page=2",
        f"Authorization: Bearer {SECRET}",
        "Incorrect API key provided: sk-or-v1-abcdef0123456789",
        f"unlabelled {SECRET} from the environment",
    ],
)
def test_credentials_are_removed_from_exception_messages(log_lines, monkeypatch, message):
    if "unlabelled" in message:
        monkeypatch.setenv("SOME_API_KEY", SECRET)

    log_failure("llm.failed", RuntimeError(message))

    text = json.dumps(log_lines())
    assert "RuntimeError" in text
    assert SECRET not in text
    assert "abcdef0123456789" not in text


def test_validation_errors_do_not_quote_the_input(log_lines):
    class Reply(BaseModel):
        count: int

    with pytest.raises(ValueError) as error:
        Reply.model_validate({"count": "the user's private text"})
    log_failure("llm.failed", error.value)

    text = json.dumps(log_lines())
    assert "private text" not in text
    assert "count: int_parsing" in text


def test_request_urls_logged_by_httpx_stay_out_of_the_log(log_lines):
    logging.getLogger("httpx").info("HTTP Request: GET https://x/skus?key=abc")

    assert log_lines() == []
