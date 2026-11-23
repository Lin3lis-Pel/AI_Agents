# Progress

Last updated: 2026-11-23

| Phase | Status | Notes |
|---|---|---|
| 0 Project setup | Done | `uv sync`, layout, ruff, pytest |
| 1 Deterministic tools | Done | Parser, 20 rules, cost estimator, Billing API client, dataset of 14 samples |
| 2 RAG | Done | 34 documents, 68 chunks, local embeddings |
| 3 Memory | Done | SQLite store, feedback policy |
| 4 Agent graph | Done | 10 nodes, rewrite loop capped at 2, interrupt for human review |
| 5 Interfaces | Done | MCP tool, and a Next.js web UI over an HTTP API (replaced Streamlit); browser tests |
| 6 Evaluation | Done | Deterministic run, 3-model comparison with DeepEval and Ragas, MiMo ablations, graph against a ReAct baseline (all in `evals/results/`) |
| 7 Documentation | Done | README results, charts and discussion written from the saved runs |

## Done on 2026-11-23 (hardening)

- Billing API boundary: any failure is a fallback, per service; total time limit; key in a
  header; live prices for all five services (decision 20).
- Total time limit per LLM step and per review (decision 21).
- Structured logs with review and request ids; the cause of every failure is logged,
  redacted (decision 22).
- One usage summary (`build_report`), one token price calculation (`usage_cost_usd`), one
  place that validates input (the entry points).
- Web UI: a broken connection, an unreadable line, a stream that ends early and a failed
  example list are all shown, and the page can always be used again.
- TypeScript types generated from the API schema; Playwright tests (decision 23).
- CI with coverage floors (decision 24).
- Evals: `no_grade`, two report prompts, 21 hard queries, a rewrite-loop eval, a price
  check, and a held-out set of 35 files labelled by Checkov (recall 1.00, precision 0.88;
  decision 25). The free ones have been run.
- Documentation: Streamlit and FastMCP leftovers removed, feedback wording made the same
  everywhere.

## Done on 2026-11-10

- First commit and push to GitHub (`main`).
- Checked with real keys: OpenRouter (JSON mode, temperature, seed, tool calling), the
  DeepEval and Ragas judges, LangSmith tracing and the live Billing Catalog API.
- Static prices checked against the live API. Compute Engine and disk prices for 8
  regions are now exact (decision 15). Expected costs recalculated by hand.
- Cost estimate: `default_region` and `regions_used` instead of one misleading
  `region`; multi-region buckets say how they were priced. `regions_used` reports the
  region whose prices were actually used (architecture candidate 6).
- Default model is `google/gemini-3.5-flash-lite`, chosen from the comparison.
- README: common use cases with a real saved review, a "what kind of agent" paragraph.
  Learning notes: function calling, agent types, prompt engineering against RAG against
  agents.
- Ragas metrics for the RAG step (decision 17).
- ReAct baseline with function calling, compared with the graph agent (decision 16).
- Eval runner: commit recorded at run start, judged text saved, `evals/rejudge.py`,
  flushed progress output (decision 18).
- Six-panel comparison charts for the models and for graph against ReAct.
- Architecture review: `docs/reviews/architecture-2026-11-10.md`, with a status update.
- Web UI rebuilt in Next.js (decision 19): review service, HTTP API, live progress
  steps, an intro that says what the tool does and does not do. Streamlit removed.
  Checked by clicking through it in a real browser.
- README: run date and commit removed from the result source lines; first-person wording.
- Security rules, found by trying configurations outside the dataset: GKE-005 now reads
  node pools declared inside a cluster and treats `service_account = "default"` as the
  default account (GCE-003 too). Remediation text now fits the resource: GKE-001 with an
  open network, Cloud Run v1 against v2 secrets, Cloud SQL links per engine. Regression
  tests in `tests/tools/test_security_rules.py`.
- "What the LLM adds": `evals/llm_value.py` compares a review with no LLM against the
  saved three-model run, with no model calls. README has the table, examples and the
  explanation of the three model calls. The rule-only version of the example review is
  `docs/examples/web_stack_review_no_llm.md`.

## Saved runs the README uses

| File in `evals/results/` | What it is |
|---|---|
| `20261123T213249Z_deterministic.json` | Rules (also on the held-out set), cost estimator, retrieval (also on hard queries), tool speed. Saved from uncommitted code (`dirty: true`) |
| `20261123T211900Z_price_check.json` | Static prices against the live Billing Catalog. Same remark |
| `20261110T141250Z_llm.json` | Three models, graph agent, DeepEval and Ragas (judge GPT-5 mini) |
| `20261110T080559Z_llm.json` | MiMo ablations: full, no RAG, no rewrite loop (judge MiMo-pro) |
| `20261110T115819Z_llm.json` | Graph agent against the ReAct baseline on Gemini |
| `20261110T095622Z_llm.json` | First three-model run, without Ragas; cited for run-to-run variance and the 628 s fallback |
| `20261110T141250Z_ragas_diagnostic.json` | Check of why Ragas faithfulness is low |
| `20261110T175350Z_llm_value.json` | Report with and without the LLM, built from `20261110T141250Z_llm.json` with no model calls |

OpenRouter spend for all runs: under $3 of the $5 budget.

## What is left (optional)

1. **SQL-002 when `ip_configuration` is missing:** find out whether Cloud SQL then gives a
   public IP. If the rule changes, the seven held-out files become regression tests. Human
   labels for the 5 rules Checkov does not cover.
2. **Paid runs, to be confirmed first:** `--configs full,no_grade`,
   `--configs full,prompt_specific,prompt_short`, and `evals.rewrite_loop`. The code and the
   README tables are ready; no run has been made, so there is no number yet.
3. **Architecture candidate 1:** LLM fallbacks as structured data instead of warning text.
4. A premium model in the comparison, and ablations on a second model.
5. The ReAct baseline on more models and with harder injection samples.
6. Exact regional static prices for Cloud Storage, Cloud Run and Cloud SQL. This changes
   three hand-calculated expected costs.
7. Move the MCP server onto `agent/service.py`, so both front ends share one path.
8. LangGraph warns that the checkpointer will refuse unregistered types in a future
   version; our state models need to be on its allow list before that upgrade.

## Verification at the time of writing

- `uv run pytest`: all tests pass, with no API key (the LLM is faked at the boundary).
- `npm run test:e2e`: the browser tests pass (run locally with Microsoft Edge).
- The CI workflow has not run yet: it starts with the first push.
- `uv run ruff check .`: no errors.
