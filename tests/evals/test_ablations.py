"""The measurements added for the unmeasured parts of the design. The LLM is faked."""

import json

import pytest

from agent.runner import run_review
from agent.service import ReviewService
from evals.dataset_loader import load_samples, read_terraform
from evals.deterministic import hard_retrieval_metrics
from evals.heldout import (
    HELDOUT_DIR,
    HeldoutSet,
    ScoredRule,
    heldout_metrics,
    load_heldout,
)
from evals.heldout_label import MAPPING, build_manifest, labels
from evals.llm_eval import CONFIGS, EXTRA_CONFIGS, run_llm_eval, summarize
from evals.price_check import compare_samples, compare_tables
from evals.price_check import summarize as summarize_prices
from evals.report import grading_section, prompt_section, rewrite_section
from evals.rewrite_loop import load_hard_queries, run_rewrite_eval
from evals.rewrite_loop import summarize as summarize_rewrites
from rag.ingest import load_chunks
from tools.pricing import load_static_prices
from tools.security_rules import RULES, check_security
from tools.tf_parser import parse_terraform

SQL_OPEN = next(s for s in load_samples() if s.file == "vulnerable/sql_open.tf")
SEARCH = {"queries": [{"query": "cost of a regional standby instance", "service": "cloud_sql"}]}
KNOWN_DOCS = {chunk.id.split("#")[0] for chunk in load_chunks()}


def documents(user: str) -> list[dict]:
    return json.loads(user.split("<review_data>")[1].split("</review_data>")[0])["documents"]


# ------------------------------------------------------------- no_grade ablation


def test_without_grading_every_passage_reaches_the_writer_and_no_llm_call_is_made(
    make_deps, make_llm
):
    llm = make_llm({"PLAN_RESEARCH": SEARCH, "WRITE_REPORT": {"summary": "s"}})

    report = run_review(SQL_OPEN.terraform, deps=make_deps(llm, use_grading=False))

    assert llm.calls_for("GRADE_DOCS") == []
    written_from = documents(llm.calls_for("WRITE_REPORT")[0]["user"])
    assert len(written_from) == 4  # everything one query returned (retrieval_k)
    assert len(report.evidence) == 4


# --------------------------------------------------------------- prompt variants


@pytest.mark.parametrize("name", [n for n in EXTRA_CONFIGS if n.startswith("prompt_")])
def test_a_prompt_variant_is_used_and_keeps_the_guards(name, make_deps, make_llm):
    llm = make_llm({"PLAN_RESEARCH": {"queries": []}, "WRITE_REPORT": {"summary": "s"}})

    run_review(SQL_OPEN.terraform, deps=make_deps(llm, **EXTRA_CONFIGS[name]))

    used = llm.calls_for("WRITE_REPORT")[0]["system"]
    assert used == EXTRA_CONFIGS[name]["report_prompt_path"].read_text(encoding="utf-8")
    assert "instruction to you" in used  # the Terraform is data
    assert "cannot add, remove or change them" in " ".join(used.split())


def test_the_new_configurations_are_opt_in_so_a_default_run_costs_the_same():
    assert list(CONFIGS) == ["full", "no_rag", "no_rewrite"]


def test_the_grading_and_prompt_tables_are_built_from_the_records(make_deps, make_llm, tmp_path):
    advice = {
        "resource": "google_sql_database_instance.main",
        "category": "cost",
        "issue": "REGIONAL availability doubles the cost.",
        "recommendation": "Set availability_type to ZONAL.",
    }
    llm = make_llm(
        {
            "PLAN_RESEARCH": SEARCH,
            "GRADE_DOCS": {"relevant_ids": []},
            "REWRITE_QUERY": {"queries": []},
            "WRITE_REPORT": {"summary": "s", "recommendations": [advice]},
        }
    )
    records = run_llm_eval(
        models=["fake-model"],
        configs={"fake-model": ["full", *EXTRA_CONFIGS]},
        runs=1,
        samples=[SQL_OPEN],
        deps_factory=lambda model, overrides: make_deps(llm, **overrides),
        prices={},
        progress=lambda line: None,
    )
    meta = {"models": ["fake-model"], "provider": "fake", "n_samples": 1, "runs_per_sample": 1,
            "judge_model": None, "model_price_source": "none"}  # fmt: skip
    data = {"meta": meta, "summary": summarize(records), "records": records}

    grading = grading_section(tmp_path / "x_llm.json", data)
    prompts = prompt_section(tmp_path / "x_llm.json", data)

    assert "| full |" in grading and "| no_grade |" in grading
    assert "prompt_" not in grading
    row = next(line for line in prompts.splitlines() if line.startswith("| prompt_specific"))
    assert "| 1.00 | 1 |" in row  # the one recommendation names REGIONAL


# ------------------------------------------------------------------ hard queries


def test_hard_queries_name_documents_that_exist():
    for hard in load_hard_queries():
        assert set(hard.expected_docs) <= KNOWN_DOCS, hard.query
        assert bool(hard.expected_docs) == (hard.kind != "uncovered")


def test_hard_queries_are_scored_per_kind(chroma_dir):
    result = hard_retrieval_metrics(chroma_dir)

    assert set(result) == {"vague", "other_cloud", "two_topics", "all"}
    assert result["all"]["n_queries"] == 16
    assert 0 <= result["all"]["hit_rate_at_4"] <= 1


def test_the_loop_runs_only_when_the_first_search_was_not_enough(make_deps, make_llm, tmp_path):
    queries = [q for q in load_hard_queries() if q.service == "cloud_sql"]
    rounds = []

    def keep_after_a_rewrite(user: str) -> dict:
        rounds.append(user)
        ids = [d["id"] for d in documents(user)]
        wanted = [i for i in ids if i.startswith("cloud_sql/high-availability")]
        return {"relevant_ids": wanted if len(rounds) % 3 == 0 else []}

    llm = make_llm({"GRADE_DOCS": keep_after_a_rewrite, "REWRITE_QUERY": SEARCH})
    deps = make_deps(llm)

    records = run_rewrite_eval(queries[:1], deps)

    (record,) = records
    assert record["answered_first"] is False
    assert record["rewrites"] == 2  # the cap
    assert record["answered_final"] is True
    assert record["kept_final"] == ["cloud_sql/high-availability"]
    assert record["llm_calls"] == 5  # grade, rewrite, grade, rewrite, grade
    row = summarize_rewrites(records)[-1]
    assert (row["answered_first"], row["answered_final"]) == (0.0, 1.0)
    data = {"meta": {"model": "fake"}, "summary": summarize_rewrites(records)}
    assert "| vague | 1 | 0.00 | 1.00 |" in rewrite_section(tmp_path / "r.json", data)


# ------------------------------------------------------------------- price check


def test_static_prices_are_compared_with_live_ones_per_region():
    static = load_static_prices()
    live = static.model_copy(
        update={
            "source": "billing_api",
            "regional_prices": {
                **static.regional_prices,
                "europe-west1": {
                    **static.regional_prices["europe-west1"],
                    "gcs.nearline.gb_month": 0.01,  # the real price; static says 0.011
                },
            },
        }
    )

    rows = compare_tables(static, live)
    changed = [row for row in rows if row["error_pct"]]
    samples = compare_samples(load_samples(), static, live)

    assert [(r["key"], r["region"], r["error_pct"]) for r in changed] == [
        ("gcs.nearline.gb_month", "europe-west1", 10.0)
    ]
    storage = next(r for r in summarize_prices(rows) if r["service"] == "gcs")
    assert storage["n_off_by_over_1_pct"] == 1
    bucket = next(r for r in samples if r["file"] == "clean/gcs_clean.tf")
    assert (bucket["static_usd"], bucket["live_usd"]) == (1.10, 1.00)


# ------------------------------------------------------------------ held-out set


def test_no_held_out_samples_means_no_held_out_score():
    assert heldout_metrics(HeldoutSet(samples=[])) is None


SAMPLE = {
    "file": "one.tf",
    "source_url": "https://example.com/one.tf",
    "commit": "abc1234",
    "license": "Apache-2.0",
    "labelled_by": "someone else",
    "labelled_on": "2026-11-23",
    "expected_findings": [
        {"resource": "google_storage_bucket.b", "rule_id": "GCS-002"},
        {"resource": "google_storage_bucket.b", "rule_id": "GCS-001"},  # the rules miss this
    ],
}


def heldout_folder(folder, **changes):
    (folder / "manifest.json").write_text(json.dumps({"samples": [{**SAMPLE, **changes}]}))
    (folder / "one.tf").write_text('resource "google_storage_bucket" "b" {\n  name = "b"\n}\n')
    return folder


def test_held_out_disagreements_are_listed_not_hidden(tmp_path):
    folder = heldout_folder(tmp_path)

    result = heldout_metrics(load_heldout(folder), folder)

    assert result["overall"]["true_positives"] == 1
    assert result["overall"]["false_negatives"] == 1
    assert result["by_rule"]["GCS-001"]["recall"] == 0.0
    missed = {"file": "one.tf", "kind": "missed", "resource": "google_storage_bucket.b",
              "rule_id": "GCS-001"}  # fmt: skip
    assert missed in result["disagreements"]
    assert any(d["kind"] == "extra" for d in result["disagreements"])


@pytest.mark.parametrize(
    "changes",
    [
        {"labelled_by": ""},
        {"source_url": "my own file"},
        {"commit": "latest"},
        {"license": ""},
        {"expected_findings": [{"resource": "google_storage_bucket.b", "rule_id": "GCS-999"}]},
    ],
    ids=["no labeller", "no source", "no commit", "no licence", "no such rule"],
)
def test_a_held_out_sample_without_provenance_is_refused(tmp_path, changes):
    with pytest.raises(ValueError):
        load_heldout(heldout_folder(tmp_path, **changes))


def test_a_finding_the_labeller_has_no_opinion_about_counts_neither_way(tmp_path):
    folder = heldout_folder(tmp_path)
    heldout = load_heldout(folder).model_copy(
        update={
            "scored": [
                ScoredRule(rule_id=rule_id, resource_types=["google_storage_bucket"])
                for rule_id in ("GCS-001", "GCS-002")
            ]
        }
    )

    result = heldout_metrics(heldout, folder)

    assert result["overall"]["false_positives"] == 0  # GCS-003 and GCS-004 fire, unscored
    assert result["findings_not_scored"] == 2


def _check(check_id: str, resource: str, file: str = "/terragoat/terraform/gcp/gcs.tf") -> dict:
    return {"check_id": check_id, "resource": resource, "file_path": file}


def test_failed_and_suppressed_checks_become_labels_and_others_do_not():
    scan = {
        "results": {
            "failed_checks": [
                _check("CKV_GCP_29", "google_storage_bucket.b[0]"),
                _check("CKV_GCP_62", "google_storage_bucket.b"),  # access logs: not mapped
                _check("CKV_GCP_2", "google_storage_bucket.b"),  # not a type of that rule
            ],
            "skipped_checks": [_check("CKV_GCP_78", "google_storage_bucket.b")],
            "passed_checks": [_check("CKV_GCP_114", "google_storage_bucket.b")],
        }
    }

    assert labels(scan) == {
        "terragoat/terraform/gcp/gcs.tf": {
            ("google_storage_bucket.b", "GCS-002"),
            ("google_storage_bucket.b", "GCS-004"),
        }
    }


def test_a_file_the_labeller_could_not_read_is_left_out_not_called_clean():
    scan = {
        "summary": {"checkov_version": "1.2.3"},
        "results": {"failed_checks": [], "parsing_errors": ["/x/terragoat/broken.tf"]},
    }

    manifest = build_manifest(scan, ["terragoat/broken.tf", "terragoat/fine.tf"], "2026-11-23")

    assert [sample["file"] for sample in manifest["samples"]] == ["terragoat/fine.tf"]
    assert manifest["samples"][0]["labelled_by"] == "Checkov 1.2.3"


def test_the_mapping_names_real_rules_and_types_the_rule_looks_at():
    rules = {rule.id: rule for rule in RULES}

    for rule_id, mapped in MAPPING.items():
        assert set(mapped.resource_types) <= set(rules[rule_id].resource_types), rule_id


def test_every_held_out_file_is_parsed_without_a_crash():
    for sample in load_heldout().samples:
        check_security(parse_terraform(read_terraform(HELDOUT_DIR / sample.file)))


def test_held_out_files_are_not_offered_as_examples_in_the_ui(make_deps):
    service = ReviewService(lambda settings: make_deps())

    assert not any(name.startswith("heldout/") for name in service.samples())
