"""The held-out files are chosen by a fixed rule, from git, and copied unchanged."""

import json
import subprocess

import pytest

from evals import heldout_label, heldout_select
from evals.heldout import load_heldout

BUCKET = 'resource "google_storage_bucket" "b" {\n  name = "b"\n}\n'
TOPIC = 'resource "google_pubsub_topic" "t" {\n  name = "t"\n}\n'
TERRAGOAT_FILES = {
    "terraform/gcp/gcs.tf": BUCKET,
    "terraform/gcp/variables.tf": 'variable "x" {}\n',  # no resource
    "terraform/aws/s3.tf": BUCKET,  # another folder
}
GOOGLE_FILES = {
    **{f"storage/example_{n}/main.tf": BUCKET for n in range(8)},
    "storage/topic/main.tf": TOPIC,  # no supported type
    "storage/example_0/outputs.tf": BUCKET,  # not a main.tf
    "run/wrong_service/main.tf": BUCKET,  # a supported type of another service
}


def make_clone(folder, files: dict[str, str]) -> str:
    """A git repository with `files` at its first commit. Returns the commit."""
    for path, text in {**files, "LICENSE": "Apache License\n"}.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())

    def git(*args: str) -> str:
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        command = ["git", "-C", str(folder), *identity, "-c", "core.autocrlf=false", *args]
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()

    git("init", "--quiet")
    git("add", "--all")
    git("commit", "--quiet", "-m", "files")
    return git("rev-parse", "HEAD")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Two small repositories in place of the real ones, and an empty held-out folder."""
    clones, target = tmp_path / "clones", tmp_path / "heldout"
    for name, files in (("TERRAGOAT", TERRAGOAT_FILES), ("GOOGLE", GOOGLE_FILES)):
        source = getattr(heldout_select, name)
        commit = make_clone(clones / heldout_select.CLONES[source.name], files)
        monkeypatch.setattr(heldout_select, name, source.model_copy(update={"commit": commit}))
    target.mkdir()
    monkeypatch.setattr(heldout_select, "HELDOUT_DIR", target)
    monkeypatch.setattr(heldout_label, "HELDOUT_DIR", target)
    return clones, target


def chosen(target) -> set[str]:
    return {p.relative_to(target).as_posix() for p in target.rglob("*.tf")}


def test_only_files_with_a_supported_resource_of_the_right_service_are_taken(sources):
    clones, target = sources

    assert heldout_select.main([str(clones)]) == 0

    files = chosen(target)
    assert {f for f in files if f.startswith("terragoat/")} == {"terragoat/terraform/gcp/gcs.tf"}
    google = {f for f in files if f.startswith("google-docs-samples/")}
    assert len(google) == heldout_select.PER_SERVICE
    assert all(f.startswith("google-docs-samples/storage/example_") for f in google)
    assert all(f.endswith("/main.tf") for f in google)


def test_the_choice_is_the_same_every_time_and_the_files_are_unchanged(sources):
    clones, target = sources
    heldout_select.main([str(clones)])
    first = chosen(target)

    heldout_select.main([str(clones)])

    assert chosen(target) == first
    assert (target / "terragoat/terraform/gcp/gcs.tf").read_bytes() == BUCKET.encode()
    assert (target / "terragoat/LICENSE").exists()


def test_a_scan_becomes_a_manifest_that_the_loader_accepts(sources, tmp_path):
    clones, target = sources
    heldout_select.main([str(clones)])
    failed = {
        "check_id": "CKV_GCP_29",
        "resource": "google_storage_bucket.b",
        "file_path": "\\terragoat\\terraform\\gcp\\gcs.tf",  # as Checkov writes it on Windows
    }
    scan = {"summary": {"checkov_version": "3.4.7"}, "results": {"failed_checks": [failed]}}
    (tmp_path / "scan.json").write_text(json.dumps(scan), encoding="utf-8")

    assert heldout_label.main([str(tmp_path / "scan.json")]) == 0

    heldout = load_heldout(target)
    assert len(heldout.samples) == 1 + heldout_select.PER_SERVICE
    terragoat = next(s for s in heldout.samples if s.file.startswith("terragoat/"))
    assert [e.rule_id for e in terragoat.expected_findings] == ["GCS-002"]
    assert len(heldout.scored) == len(heldout_label.MAPPING)


def test_a_scan_of_other_files_is_refused():
    scan = {
        "summary": {"checkov_version": "3.4.7"},
        "results": {
            "failed_checks": [
                {
                    "check_id": "CKV_GCP_29",
                    "resource": "google_storage_bucket.b",
                    "file_path": "/somewhere/else.tf",
                }
            ]
        },
    }

    with pytest.raises(ValueError, match="not in the set"):
        heldout_label.build_manifest(scan, ["terragoat/a.tf"], "2026-11-23")
