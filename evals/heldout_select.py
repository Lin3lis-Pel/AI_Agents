"""Choose the held-out files. Reproducible, and blind to how the rules score on them.

    uv run python -m evals.heldout_select <folder with the two clones>

The rule was written before any of the files was opened:

- TerraGoat: every `.tf` file in `terraform/gcp/` with a resource of a supported type.
- Google's documentation samples: for each of the five services, the `main.tf` files in
  that service's folder with a resource of a supported type **of that service**, in the
  order of the SHA-256 of their path, the first `PER_SERVICE`. Hash order because the
  folders are named by topic, and alphabetical order would pick one topic.

The files are read from git at a pinned commit, matched with a regular expression, and
copied unchanged. Nothing is parsed, and the rules are not imported.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

from evals.heldout import HELDOUT_DIR
from tools.models import Service
from tools.tf_parser import SUPPORTED_TYPES

PER_SERVICE = 6
GOOGLE_FOLDERS = {
    "cloud_sql": Service.CLOUD_SQL,
    "compute": Service.COMPUTE,
    "gke": Service.GKE,
    "run": Service.CLOUD_RUN,
    "storage": Service.CLOUD_STORAGE,
}
_RESOURCE = re.compile(r'^\s*resource\s+"([a-z0-9_]+)"\s+"', re.M)


class Source(BaseModel):
    name: str  # folder in the held-out set
    repo: str
    commit: str
    license: str


TERRAGOAT = Source(
    name="terragoat",
    repo="https://github.com/bridgecrewio/terragoat",
    commit="729f8da62c6a85ce4af5ad3d123de97776d954c4",
    license="Apache-2.0",
)
GOOGLE = Source(
    name="google-docs-samples",
    repo="https://github.com/terraform-google-modules/terraform-docs-samples",
    commit="044aade264f9207e204d4e600d2e3a494a1a5d6e",
    license="Apache-2.0",
)
CLONES = {TERRAGOAT.name: "terragoat", GOOGLE.name: "terraform-docs-samples"}


def _git(clone: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, check=True).stdout


def paths(clone: Path, source: Source) -> list[str]:
    listing = _git(clone, "ls-tree", "-r", "--name-only", source.commit).decode("utf-8")
    return [path for path in listing.splitlines() if path.endswith(".tf")]


def content(clone: Path, source: Source, path: str) -> bytes:
    return _git(clone, "show", f"{source.commit}:{path}")


def services_in(text: str) -> set[Service]:
    return {SUPPORTED_TYPES[t] for t in _RESOURCE.findall(text) if t in SUPPORTED_TYPES}


def choose_terragoat(clone: Path) -> dict[str, bytes]:
    files = {
        path: content(clone, TERRAGOAT, path)
        for path in paths(clone, TERRAGOAT)
        if path.startswith("terraform/gcp/")
    }
    return {p: data for p, data in files.items() if services_in(data.decode("utf-8"))}


def choose_google(clone: Path) -> dict[str, bytes]:
    chosen: dict[str, bytes] = {}
    for folder, service in GOOGLE_FOLDERS.items():
        candidates = [
            p for p in paths(clone, GOOGLE) if p.startswith(f"{folder}/") and p.endswith("/main.tf")
        ]
        taken = 0
        for path in sorted(candidates, key=lambda p: hashlib.sha256(p.encode()).hexdigest()):
            if taken == PER_SERVICE:
                break
            data = content(clone, GOOGLE, path)
            if service in services_in(data.decode("utf-8")):
                chosen[path] = data
                taken += 1
    return chosen


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    folder = Path(argv[0])
    for source, choose in ((TERRAGOAT, choose_terragoat), (GOOGLE, choose_google)):
        clone = folder / CLONES[source.name]
        files = choose(clone)
        for path, data in {**files, "LICENSE": content(clone, source, "LICENSE")}.items():
            target = HELDOUT_DIR / source.name / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        print(f"{source.name}: {len(files)} files")
        for path in files:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
