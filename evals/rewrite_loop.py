"""Does the rewrite loop earn its place? Run the research part alone on hard queries.

    uv run python -m evals.rewrite_loop --model google/gemini-3.5-flash-lite

In a full review the first search almost always finds enough, so the loop does not run
and the ordinary evals cannot measure it. Here each hard query
(`dataset/retrieval_queries_hard.json`) is the first query, and the graph's own nodes
and routing take it from there: retrieve, grade, and rewrite up to `max_rewrites` times.
For each query the result says what the first round had kept, what was kept at the end
and how many rewrites ran, so the difference is what the loop added.

This calls a paid model, about two to six short calls per query. Confirm the model and
the budget first (CLAUDE.md, "Research purpose").
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from agent.config import Settings
from agent.deps import Deps
from agent.graph import after_rewrite, make_after_grade
from agent.llm import usage_cost_usd
from agent.nodes.grade_docs import make_grade_docs
from agent.nodes.retrieve import make_retrieve
from agent.nodes.rewrite_query import make_rewrite_query
from agent.state import ResearchQuery, ReviewState
from evals.dataset_loader import DATASET_DIR, load_samples
from evals.metrics import mean_or_none, share
from tools.models import Resource
from tools.tf_parser import parse_terraform

HARD_QUERIES = DATASET_DIR / "retrieval_queries_hard.json"


class HardQuery(BaseModel):
    kind: str
    service: str
    query: str
    expected_docs: list[str]


def load_hard_queries() -> list[HardQuery]:
    data = json.loads(HARD_QUERIES.read_text(encoding="utf-8"))
    return [HardQuery(**item) for item in data["queries"]]


def example_resources(service: str) -> list[Resource]:
    """Resources of the service, because the grader judges relevance against resources."""
    resources = [
        resource
        for sample in load_samples("clean")
        for resource in parse_terraform(sample.terraform)
    ]
    return [r for r in resources if r.service is not None and r.service.value == service]


def build_research_graph(deps: Deps) -> Any:
    """The research part of the review graph: the same nodes and the same routing."""
    settings = deps.settings
    graph = StateGraph(ReviewState)
    graph.add_node("retrieve", make_retrieve(deps))
    graph.add_node("grade_docs", make_grade_docs(deps))
    graph.add_node("rewrite_query", make_rewrite_query(deps))
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "grade_docs")
    graph.add_conditional_edges(
        "grade_docs",
        make_after_grade(settings.min_relevant_docs, settings.max_rewrites),
        {"rewrite_query": "rewrite_query", "write_report": END},
    )
    graph.add_conditional_edges(
        "rewrite_query", after_rewrite, {"retrieve": "retrieve", "write_report": END}
    )
    return graph.compile()


def _doc_names(state: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(doc.id.split("#")[0] for doc in state["relevant_docs"]))


def run_query(graph: Any, hard: HardQuery, deps: Deps) -> dict[str, Any]:
    """One record: what round one kept, and what the loop ended with."""
    start = ReviewState(
        terraform="",
        review_id=uuid.uuid4().hex[:12],
        resources=example_resources(hard.service),
        queries=[ResearchQuery(query=hard.query, service=hard.service)],
        deadline_at=deps.clock() + deps.settings.review_deadline_s,
    )
    rounds: list[list[str]] = []  # documents kept after each grading
    final: dict[str, Any] = {}
    for mode, data in graph.stream(start, stream_mode=["updates", "values"]):
        if mode == "values":
            final = data
        elif "grade_docs" in data:
            rounds.append([])
        if mode == "values" and rounds:
            rounds[-1] = _doc_names(data)
    usage = final.get("usage", [])
    first, last = (rounds[0], rounds[-1]) if rounds else ([], [])
    expected = set(hard.expected_docs)
    return {
        **hard.model_dump(),
        "kept_first": first,
        "kept_final": last,
        "answered_first": bool(expected & set(first)),
        "answered_final": bool(expected & set(last)),
        "rewrites": final.get("rewrite_count", 0),
        "queries_final": [query.query for query in final.get("queries", [])],
        "errors": final.get("errors", []),
        "llm_calls": len(usage),
        "input_tokens": sum(record.input_tokens for record in usage),
        "output_tokens": sum(record.output_tokens for record in usage),
        "cost_usd": usage_cost_usd(usage) if usage else 0.0,
    }


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per kind of query, and one for all of them."""
    rows = []
    kinds = [*dict.fromkeys(record["kind"] for record in records), "all"]
    for kind in kinds:
        group = [r for r in records if kind in (r["kind"], "all")]
        answerable = [r for r in group if r["expected_docs"]]
        uncovered = [r for r in group if not r["expected_docs"]]
        rows.append(
            {
                "kind": kind,
                "n_queries": len(group),
                "answered_first": share(
                    sum(r["answered_first"] for r in answerable), len(answerable)
                ),
                "answered_final": share(
                    sum(r["answered_final"] for r in answerable), len(answerable)
                ),
                # For a question the store cannot answer, keeping nothing is right.
                "kept_nothing_when_uncovered": share(
                    sum(1 for r in uncovered if not r["kept_final"]), len(uncovered)
                ),
                "share_rewritten": share(sum(1 for r in group if r["rewrites"]), len(group)),
                "rewrites_mean": mean_or_none(r["rewrites"] for r in group),
                "llm_calls_mean": mean_or_none(r["llm_calls"] for r in group),
                "cost_usd_mean": mean_or_none(r["cost_usd"] for r in group),
            }
        )
    return rows


def run_rewrite_eval(queries: list[HardQuery], deps: Deps) -> list[dict[str, Any]]:
    graph = build_research_graph(deps)
    return [run_query(graph, hard, deps) for hard in queries]


def main(argv: list[str] | None = None) -> int:
    from agent.deps import default_deps
    from evals.run import metadata, save

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=0, help="use only the first N queries")
    args = parser.parse_args(argv)

    meta = metadata("rewrite_loop")  # before any work: the code this run uses
    settings = Settings.from_env().model_copy(update={"model": args.model})
    deps = default_deps(settings)
    if deps.llm is None:
        print("No OPENROUTER_API_KEY or OPENAI_API_KEY set: nothing to run.")
        return 1
    queries = load_hard_queries()
    records = run_rewrite_eval(queries[: args.limit] if args.limit else queries, deps)
    path = save(
        "rewrite_loop",
        {
            "meta": {
                **meta,
                "model": args.model,
                "settings": json.loads(settings.model_dump_json(exclude={"data_dir"})),
            },
            "summary": summarize(records),
            "records": records,
        },
    )
    print(f"Saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
