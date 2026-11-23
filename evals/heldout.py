"""The held-out set: Terraform written by other people, labelled by someone else.

The 14 samples in `evals/dataset/` were written next to the rules, so their scores say
how well the rules fit their author's idea of a problem. A held-out sample says how they
do on somebody else's. `dataset/heldout/README.md` has the protocol that keeps it held
out: labels come first, from someone who does not know how the rules work, and a sample
that has been used to change a rule stops being one.

The loader refuses a sample that does not say where it came from and who labelled it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evals.dataset_loader import DATASET_DIR, ExpectedFinding, read_terraform
from evals.metrics import Confusion, confusion
from tools.security_rules import RULES, check_security
from tools.tf_parser import parse_terraform

HELDOUT_DIR = DATASET_DIR / "heldout"


class HeldoutSample(BaseModel):
    file: str
    source_url: str = Field(pattern=r"^https://")
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")  # the exact version that was labelled
    license: str = Field(min_length=1)
    labelled_by: str = Field(min_length=1)  # a person, or a tool and its version
    labelled_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    expected_findings: list[ExpectedFinding]


class ScoredRule(BaseModel):
    """A rule the labeller has an opinion about, and on which resource types."""

    rule_id: str
    resource_types: list[str]


class HeldoutSet(BaseModel):
    scored: list[ScoredRule] = Field(default_factory=list)  # empty = the labeller judged all
    samples: list[HeldoutSample]

    def scores(self, pair: tuple[str, str]) -> bool:
        resource_type, rule_id = pair[0].split(".")[0], pair[1]
        return not self.scored or any(
            rule.rule_id == rule_id and resource_type in rule.resource_types for rule in self.scored
        )


def load_heldout(folder: Path = HELDOUT_DIR) -> HeldoutSet:
    heldout = HeldoutSet.model_validate_json((folder / "manifest.json").read_text("utf-8"))
    known = {rule.id for rule in RULES}
    for sample in heldout.samples:
        unknown = {e.rule_id for e in sample.expected_findings} - known
        if unknown:
            raise ValueError(f"{sample.file}: no such rule: {', '.join(sorted(unknown))}")
    return heldout


def heldout_metrics(heldout: HeldoutSet, folder: Path = HELDOUT_DIR) -> dict[str, Any] | None:
    """Precision, recall and F1, per rule too, with every disagreement listed.

    None when there are no samples: no number is better than a number about nothing.
    """
    samples = heldout.samples
    if not samples:
        return None
    total, by_rule = Confusion(), {}
    disagreements: list[dict[str, str]] = []
    unscored = 0
    for sample in samples:
        findings = check_security(parse_terraform(read_terraform(folder / sample.file)))
        expected = {(e.resource, e.rule_id) for e in sample.expected_findings}
        reported = {(f.resource, f.rule_id or "") for f in findings}
        found = {pair for pair in reported if heldout.scores(pair)}
        unscored += len(reported - found)
        total += confusion(expected, found)
        for rule_id in {rule for _, rule in expected | found}:
            by_rule[rule_id] = by_rule.get(rule_id, Confusion()) + confusion(
                [pair for pair in expected if pair[1] == rule_id],
                [pair for pair in found if pair[1] == rule_id],
            )
        for kind, pairs in (("missed", expected - found), ("extra", found - expected)):
            disagreements += [
                {"file": sample.file, "kind": kind, "resource": resource, "rule_id": rule_id}
                for resource, rule_id in sorted(pairs)
            ]
    return {
        "n_samples": len(samples),
        "labelled_by": sorted({sample.labelled_by for sample in samples}),
        "sources": dict(Counter(sample.file.split("/")[0] for sample in samples)),
        "rules_scored": len(heldout.scored) or len(RULES),
        "findings_not_scored": unscored,
        "overall": total.scores(),
        "by_rule": {rule_id: by_rule[rule_id].scores() for rule_id in sorted(by_rule)},
        "disagreements": disagreements,
    }
