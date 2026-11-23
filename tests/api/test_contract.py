"""The report is the contract between the Python API and the Node.js UI.

The UI's TypeScript types are generated from `web/lib/openapi.json`, which is generated
from the Pydantic models. These tests fail when the saved schema no longer matches the
models; `npm run types:check` fails when the TypeScript no longer matches the schema.
"""

import json

import pytest
from fastapi.testclient import TestClient

from api.schema import SCHEMA_PATH, build_schema
from api.server import create_app

SCHEMA = build_schema()
MODELS = SCHEMA["components"]["schemas"]


def test_the_saved_schema_is_the_one_the_api_has_now():
    saved = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert saved == SCHEMA, "run `uv run python -m api.schema`, then `npm run types` in web/"


def test_the_schema_describes_the_stream_as_one_of_the_three_events():
    content = SCHEMA["paths"]["/api/reviews"]["post"]["responses"]["200"]["content"]
    events = content["application/x-ndjson"]["schema"]

    assert {ref["$ref"].split("/")[-1] for ref in events["oneOf"]} == {
        "StepEvent",
        "ReportEvent",
        "ProblemEvent",
    }


@pytest.mark.parametrize("name", ["Finding", "ReviewReport", "UsageSummary", "CostEstimate"])
def test_an_answer_has_every_field_so_the_ui_never_sees_undefined(name):
    """A field that was silently missing crashed the page once."""
    assert set(MODELS[name]["required"]) == set(MODELS[name]["properties"])


def test_nullable_fields_say_so():
    savings = MODELS["Finding"]["properties"]["estimated_savings"]

    assert {option["type"] for option in savings["anyOf"]} == {"number", "null"}


def test_options_a_client_may_leave_out_stay_optional():
    assert "required" not in MODELS["ReviewOptions"]
    assert MODELS["ReviewRequest"]["required"] == ["terraform"]


def test_the_schema_is_served_by_the_api_itself(make_deps):
    from agent.service import ReviewService

    client = TestClient(create_app(ReviewService(lambda settings: make_deps())))

    assert set(client.get("/api/openapi.json").json()["components"]["schemas"]) == set(MODELS)
