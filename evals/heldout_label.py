"""Turn a Checkov scan of the held-out files into their labels.

    uvx --python 3.11 --from checkov==3.4.7 python -m checkov.main \\
        -d evals/dataset/heldout --framework terraform --skip-download -o json > scan.json
    uv run python -m evals.heldout_label scan.json

Checkov is an independent scanner: its authors have never seen our rules. It is not a
dependency of this project; it runs once, outside it, and only its answers are kept.

`MAPPING` was written on 2026-11-23 from the titles of Checkov's checks and the rule table
in the README, before the first scan. It says which checks mean the same as a rule, and on
which resource types Checkov looks. A pair outside it is not scored, neither for nor
against us: Checkov saying nothing about a resource it does not check is not a label.

Not mapped, because no check asks the same question:

- SQL-005 (deletion protection): none for Cloud SQL.
- GCE-002 (database ports): CKV_GCP_88 is MySQL only, our rule has six databases.
- RUN-002 (secret in a plain variable): none.
- GKE-004 (Workload Identity): CKV_GCP_69 asks about the metadata server instead.
- GKE-005 (default node service account with full access): none for GKE nodes.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evals.heldout import HELDOUT_DIR, ScoredRule
from evals.heldout_select import GOOGLE, TERRAGOAT

_INDEX = re.compile(r"\[[^\]]*\]$")  # `google_x.y[0]` is the block `google_x.y`
SOURCES = {source.name: source for source in (TERRAGOAT, GOOGLE)}


class Mapped(BaseModel):
    checks: tuple[str, ...]  # any of these failing means the rule should fire
    resource_types: tuple[str, ...]


_BUCKET = ("google_storage_bucket",)
_BUCKET_IAM = ("google_storage_bucket_iam_member", "google_storage_bucket_iam_binding")
_SQL = ("google_sql_database_instance",)
_VM = ("google_compute_instance",)
_RUN_IAM = ("google_cloud_run_service_iam_member", "google_cloud_run_service_iam_binding")
_CLUSTER = ("google_container_cluster",)

MAPPING: dict[str, Mapped] = {
    "GCS-001": Mapped(checks=("CKV_GCP_28",), resource_types=_BUCKET_IAM),
    "GCS-002": Mapped(checks=("CKV_GCP_29",), resource_types=_BUCKET),
    "GCS-003": Mapped(checks=("CKV_GCP_114",), resource_types=_BUCKET),
    "GCS-004": Mapped(checks=("CKV_GCP_78",), resource_types=_BUCKET),
    "SQL-001": Mapped(checks=("CKV_GCP_11",), resource_types=_SQL),
    "SQL-002": Mapped(checks=("CKV_GCP_60",), resource_types=_SQL),
    "SQL-003": Mapped(checks=("CKV_GCP_6",), resource_types=_SQL),
    "SQL-004": Mapped(checks=("CKV_GCP_14",), resource_types=_SQL),
    "GCE-001": Mapped(
        checks=("CKV_GCP_2", "CKV_GCP_3"), resource_types=("google_compute_firewall",)
    ),
    "GCE-003": Mapped(checks=("CKV_GCP_31",), resource_types=_VM),
    "GCE-004": Mapped(checks=("CKV_GCP_40",), resource_types=_VM),
    "RUN-001": Mapped(checks=("CKV_GCP_102",), resource_types=_RUN_IAM),
    # "not public" and "authorized networks enabled" are two halves of one question.
    "GKE-001": Mapped(checks=("CKV_GCP_18", "CKV_GCP_20"), resource_types=_CLUSTER),
    "GKE-002": Mapped(checks=("CKV_GCP_64",), resource_types=_CLUSTER),
    "GKE-003": Mapped(checks=("CKV_GCP_7",), resource_types=_CLUSTER),
}


def scored_rules() -> list[ScoredRule]:
    return [
        ScoredRule(rule_id=rule_id, resource_types=list(mapped.resource_types))
        for rule_id, mapped in MAPPING.items()
    ]


def labels(scan: dict[str, Any]) -> dict[str, set[tuple[str, str]]]:
    """`{file: {(resource, rule id)}}` for every mapped check that failed.

    A check the file's author switched off with a comment counts as failed: the comment
    changes what Checkov reports, not what the configuration does.
    """
    results = scan["results"]
    found: dict[str, set[tuple[str, str]]] = {}
    for record in [*results["failed_checks"], *results.get("skipped_checks", [])]:
        resource = _INDEX.sub("", record["resource"])
        file = record["file_path"].replace("\\", "/").lstrip("/")
        for rule_id, mapped in MAPPING.items():
            right_type = resource.split(".")[0] in mapped.resource_types
            if record["check_id"] in mapped.checks and right_type:
                found.setdefault(file, set()).add((resource, rule_id))
    return found


def build_manifest(scan: dict[str, Any], files: list[str], today: str) -> dict[str, Any]:
    version = scan["summary"]["checkov_version"]
    by_file = labels(scan)
    if unknown := set(by_file) - set(files):
        raise ValueError(f"The scan has files that are not in the set: {sorted(unknown)}")
    not_parsed = {Path(p).as_posix() for p in scan["results"].get("parsing_errors", [])}
    samples = []
    for file in files:
        if any(path.endswith(file) for path in not_parsed):
            continue  # no labels can be had from a file the labeller could not read
        source = SOURCES[file.split("/")[0]]
        path = file.split("/", 1)[1]
        samples.append(
            {
                "file": file,
                "source_url": f"{source.repo}/blob/{source.commit}/{path}",
                "commit": source.commit,
                "license": source.license,
                "labelled_by": f"Checkov {version}",
                "labelled_on": today,
                "expected_findings": [
                    {"resource": resource, "rule_id": rule_id}
                    for resource, rule_id in sorted(by_file.get(file, set()))
                ],
            }
        )
    return {
        "scored": [rule.model_dump() for rule in scored_rules()],
        "samples": samples,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    scan = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
    files = sorted(p.relative_to(HELDOUT_DIR).as_posix() for p in HELDOUT_DIR.rglob("*.tf"))
    manifest = build_manifest(scan, files, datetime.now(UTC).date().isoformat())
    (HELDOUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    count = sum(len(sample["expected_findings"]) for sample in manifest["samples"])
    print(f"{len(manifest['samples'])} of {len(files)} files labelled, {count} expected findings")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
