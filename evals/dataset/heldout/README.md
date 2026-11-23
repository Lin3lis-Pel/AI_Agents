# Held-out samples

35 files: 5 from [TerraGoat](https://github.com/bridgecrewio/terragoat) and 30 from
[Google's documentation samples](https://github.com/terraform-google-modules/terraform-docs-samples),
unmodified, each repository at a pinned commit (`manifest.json`). Both are licensed under
Apache-2.0; the licence text is in each folder.

| Step | How | File |
|---|---|---|
| Choice | Code, before any file was opened | `evals/heldout_select.py` |
| Labels | Checkov 3.4.7, through a mapping written before the scan | `evals/heldout_label.py` |
| Score | Once, on 2026-11-23, with no change to a rule or a label afterwards | `evals/heldout.py` |

Not part of this project: TerraGoat is insecure on purpose. Never apply these files.

These samples answer a question the other 14 cannot: how do the rules do on Terraform
that was not written to exercise them?

## Protocol

1. **Source.** Terraform written by other people, unmodified, from a public repository
   with a licence that allows it. Record the URL, the commit and the licence.
2. **Choice before contents.** Decide which files before reading them closely, with a
   rule that does not depend on the result (for example: every file of a repository that
   contains a supported resource type, up to a fixed number per service).
3. **Labels before rules.** The labels are written before the rules are run on the file.
4. **Labelled by someone else.** A person who has not read `tools/security_rules.py`,
   given only the rule table in the main README, or an independent scanner with a
   published mapping from its checks to these rule ids. `labelled_by` says who.
5. **Disagreements stay.** Every missed and extra finding is listed in the saved result.
   A label is changed only when it was wrong about what the file says, and the change is
   noted here. It is never changed to agree with a rule.
6. **Used once.** If a held-out file leads to a change of a rule, it becomes a regression
   test and leaves this folder: from then on the rule has seen it.

## Notes on disagreements

- **SQL-002, seven extra findings (2026-11-23).** The instances have no `ip_configuration`.
  Checkov's CKV_GCP_60 fails only when `ipv4_enabled` is written as true; the rule treats
  "not set" as public. The provider source gives `ipv4_enabled` the default true, inside an
  `ip_configuration` block that is optional and computed, so without the block the API's
  default decides. No source read said what that is. Unresolved; labels unchanged.

## `manifest.json`

`scored` lists the rules the labeller has an opinion about and the resource types it looks
at. A finding outside it counts neither for nor against the rules.

```json
{
  "scored": [{"rule_id": "GCS-002", "resource_types": ["google_storage_bucket"]}],
  "samples": [
    {
      "file": "example/main.tf",
      "source_url": "https://github.com/owner/repo/blob/<commit>/main.tf",
      "commit": "<commit>",
      "license": "Apache-2.0",
      "labelled_by": "who, or tool and version",
      "labelled_on": "2026-11-23",
      "expected_findings": [
        {"resource": "google_storage_bucket.example", "rule_id": "GCS-002"}
      ]
    }
  ]
}
```
