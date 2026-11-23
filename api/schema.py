"""Write the API's OpenAPI schema for the web UI.

    uv run python -m api.schema        # writes web/lib/openapi.json
    cd web && npm run types            # turns it into web/lib/api-schema.d.ts

The UI's TypeScript types are generated from the Pydantic models this way, so a field's
name, type and nullability cannot differ between the two languages. A test fails when
the saved schema is out of date (tests/api/test_contract.py).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent.service import ReviewService
from api.server import create_app

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "web" / "lib" / "openapi.json"
_REF = "#/components/schemas/"


def _refs(node: Any) -> Iterator[str]:
    """Names of the schemas `node` refers to."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF):
            yield ref.removeprefix(_REF)
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _sent_schemas(schema: dict[str, Any]) -> set[str]:
    """Every schema that appears in a successful answer, directly or nested."""
    schemas = schema["components"]["schemas"]
    answers = [
        operation["responses"].get("200", {})
        for path in schema["paths"].values()
        for operation in path.values()
    ]
    waiting, seen = list(_refs(answers)), set()
    while waiting:
        name = waiting.pop()
        if name not in seen:
            seen.add(name)
            waiting.extend(_refs(schemas[name]))
    return seen


def build_schema() -> dict[str, Any]:
    """The OpenAPI schema, saying that an answer always contains every field.

    Pydantic calls a field with a default optional. That is right for input. In an answer
    the field is always there (with null for "nothing"), and the UI relies on it.
    """
    # No route is called, so the service needs no database, LLM or settings.
    app = create_app(object.__new__(ReviewService))
    schema = json.loads(json.dumps(app.openapi()))
    for name in _sent_schemas(schema):
        body = schema["components"]["schemas"][name]
        if "properties" in body:
            body["required"] = list(body["properties"])
    return schema


def render() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"Wrote {SCHEMA_PATH}")
