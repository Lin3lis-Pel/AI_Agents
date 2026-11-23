# Decisions

Short records of the technical choices that shape the project. Each one says what was
decided, why, and what was given up. New decisions go at the end.

## 1. Terraform only, five services

**Decision.** The input is Terraform text. The services are Cloud Run, Compute Engine,
Cloud SQL, Cloud Storage and GKE. Other resource types are listed as "not reviewed".

**Why.** A narrow scope can be tested properly. Terraform is declarative, so the whole
configuration is visible before anything runs, which is exactly when a review helps.

**Given up.** gcloud scripts, Pulumi and the many other GCP services.

## 2. Security findings are deterministic

**Decision.** Security findings come from fixed rules in code. The LLM cannot create,
remove or change them, and neither can feedback or memory.

**Why.** A security review has to give the same answer every time, be explainable line
by line, and be immune to text hidden in the file under review. An LLM offers none of
those. Rules also cost nothing and run in well under a millisecond.

**Given up.** Rules only catch what someone wrote a rule for. An LLM might notice an
unusual problem that no rule covers. The advisory section can still mention such
things, but only as cost, sizing or simplification suggestions.

## 3. The LLM never sees the raw Terraform

**Decision.** Prompts contain a summary built from a fixed list of sizing attributes
per resource type. Free-text fields are never included.

**Why.** Defence in depth against prompt injection. Removing the text is stronger than
telling the model to ignore it. It also keeps prompts short and cheap.

**Given up.** The LLM cannot comment on anything outside the list, for example a
startup script. Adding an attribute means editing `agent/summary.py`.

## 4. JSON mode with our own validation, not provider tool calling

**Decision.** Each LLM step asks for a JSON object, and `agent/llm.py` validates it
against a Pydantic model, with one repair attempt.

**Why.** It works the same way across vendors behind OpenRouter, and with the fake
model used in tests. Validation is in one place, and the schema itself is a guard: the
report schema has no "security" category and no "critical" severity.

**Given up.** Native structured outputs can be stricter on models that support them.

## 5. LangGraph for orchestration

**Decision.** The review is a LangGraph state graph with typed (Pydantic) state.

**Why.** The flow has a bounded loop and a pause for human input. LangGraph provides
both (conditional edges, `interrupt` with a checkpointer) and makes the control flow a
visible structure that can be tested without an LLM.

**Given up.** A plain function pipeline would be less code for the straight path, but
the loop and the pause would have to be written by hand.

## 6. Chroma with local embeddings

**Decision.** Chroma as the vector store, with its default local embedding model
(all-MiniLM-L6-v2).

**Why.** Runs in-process from a folder, with no server. Local embeddings mean retrieval
needs no API key, costs nothing and gives the same result on every run, which matters
for reproducible evals. The store is small (68 chunks), so a larger system such as
pgvector would add operations work for no gain.

**Given up.** Hosted embedding models are stronger on hard queries. On this corpus the
local model already finds the expected document for nearly every test query.

## 7. Documentation as curated notes

**Decision.** `rag/docs/` holds short notes written for this project, one file per
Google Cloud page, each with the page's URL.

**Why.** Scraped pages are long and full of navigation and unrelated sections, which
hurts retrieval. Short single-topic sections make clean chunks. The URL is still the
official page, so a citation leads the reader to the authority.

**Given up.** The notes can go out of date and are only as good as their author. They
are a summary, not the source.

## 8. SQLite for memory

**Decision.** One SQLite file holds project constraints, feedback and review history.

**Why.** Single user, single machine, a handful of rows per review. No server, and the
standard library is enough.

**Given up.** Concurrent multi-user access. A hosted deployment would need Postgres.

## 9. Feedback can only deprioritise

**Decision.** Dismissing a cost, sizing or simplification suggestion marks later
suggestions of the same kind as deprioritised and sorts them last. Nothing is removed. Feedback on a
security finding is stored but has no effect.

**Why.** The user should be able to say "not relevant here" without being able to
switch off the reviewer. Keeping the item visible means a wrong dismissal is not
silent.

**Given up.** A user cannot fully hide a suggestion they are tired of.

## 10. OpenRouter as the LLM provider

**Decision.** The agent uses OpenRouter when `OPENROUTER_API_KEY` is set, and OpenAI
directly otherwise. Both go through `langchain-openai`.

**Why.** One key gives access to models from several vendors, which the model
comparison needs. OpenRouter speaks the OpenAI API, so no new dependency was added. It
also publishes prices per model, which the eval runner records with each run.

**Given up.** One more company in the request path, and small differences in which
parameters each model honours (temperature and seed are sent, and a model that does
not support them ignores them).

## 11. Static prices first, live prices for Compute Engine only

*Extended by decision 20: live prices now cover all five services.*

**Decision.** The estimator works from `tools/prices_static.json`. When a billing API
key is set, live Compute Engine prices (vCPU, memory, disks) replace the static ones.
Evals always use the static table.

**Why.** The agent must work with no credentials. Compute Engine SKUs are the simplest
to map reliably and also price GKE nodes. Static prices in evals keep cost results
identical from run to run.

**Given up.** Live prices for Cloud SQL, Cloud Storage and Cloud Run. The report says
when prices may be outdated.

## 12. Human review as a graph interrupt

**Decision.** `human_review` calls `interrupt()`. The UI resumes the graph with the
feedback. The MCP tool and the evals run with `interactive=False` and skip the pause.

**Why.** The pause is part of the agent's flow and is saved by the checkpointer, so
the review continues from where it stopped. A coding agent calling the MCP tool needs
an answer in one call, so the pause is optional.

**Given up.** The checkpointer is in memory, so a paused review is lost if the UI
process restarts.

## 13. MCP SDK 2.x

**Decision.** The server uses `MCPServer` from the MCP Python SDK 2.x.

**Why.** It is the current SDK. `FastMCP`, named in the original project notes, was
renamed to `MCPServer` in version 2. The decorator API is the same.

## 14. Three low-cost models, judged by a fourth vendor

**Decision.** The model comparison uses `xiaomi/mimo-v2.6-flash`,
`deepseek/deepseek-v4-flash` and `google/gemini-3.5-flash-lite`, judged by
`openai/gpt-5-mini`. The ablations were run once, on MiMo-flash only.

**Why.** I chose these models and a $5 budget for all runs. Three
vendors give the comparison some spread. A judge from a fourth vendor means no model
is scored by a relative. The full set of runs cost under $1.

**Given up.** No premium model, so the results cannot say what paying more buys. The
ablation run used `xiaomi/mimo-v2.6-pro` as judge (a relative of the judged model)
because it ran before the judge was chosen; it was not repeated, to save cost.

## 15. Exact regional prices in the static table

**Decision.** `tools/prices_static.json` stores exact Compute Engine and disk prices
for its 8 regions, copied from the live Billing Catalog API on 2026-11-10. Other
services keep a us-central1 price times a region multiplier.

**Why.** A check against the live API showed the hand-typed us-central1 prices were
right, but the 1.1 Europe multiplier overpriced disks by 10%: Google charges the same
disk price in us-central1 and europe-west1. Exact values remove that error for the
resources the API covers, while the evals still use a fixed table.

**Given up.** The snapshot does not update itself. It has to be refreshed by hand,
and the expected costs in `evals/dataset/` recalculated.

## 16. A ReAct baseline, in the evals only

**Decision.** `evals/react_baseline.py` is a ReAct agent with function calling over the
same three tools as the graph. It is run only by the eval suite (`--configs full,react`).
The UI and the MCP tool use the graph.

**Why.** "A fixed graph is safer than a ReAct loop" was an argument, not a
measurement. A baseline that returns the same `ReviewReport` lets the same metrics
answer it. Keeping it in `evals/` leaves the product's architecture unchanged.

**Given up.** The baseline ran on one model, and it has no hardening of its own (no
check of its answer), so it shows what an unguarded loop does, not the best a ReAct
design could do.

## 17. Ragas for the RAG step, next to DeepEval

**Decision.** Ragas scores two things about the RAG step: context precision and
faithfulness of the recommendations to the retrieved passages. DeepEval keeps judging
the whole advisory text. Both use the same judge model. Ragas response relevancy is not
used: it needs an embeddings model, and DeepEval answer relevancy covers the question.

**Why.** DeepEval's faithfulness is given everything the writer saw, so it cannot say
whether the retrieved passages were useful. Ragas looks at the passages alone, which is
what separates the graph's graded passages from the baseline's ungraded ones.

**Given up.** Ragas faithfulness against passages alone counts correct statements about
the reviewed file as unsupported (about 0.5 measured, 0.87 once the resource facts are
added). The definition was kept as first written and the caveat is in the README.
Adding Ragas also caps `langchain-community` below 0.4.2 and resolves the `openai` SDK
to 3.3.0.

## 18. Judged text is saved with each record

**Decision.** Every judged eval record keeps the text the judges were given, and
`evals/rejudge.py` scores a saved result file again with another judge.

**Why.** The first runs kept only scores. Changing the judge, and later adding Ragas,
each meant paying for the reviews again. Result files also record the git commit taken
when the run started, so a commit made during a long run cannot mislabel it.

**Given up.** Result files are larger. Files saved before this change cannot be judged
again.

## 19. A Next.js web UI over a review service and an HTTP API

**Decision.** The Streamlit UI was replaced by a Next.js app in `web/`. It talks to a
FastAPI adapter (`api/server.py`), which talks to one module, `agent/service.py`. The
service holds everything a front end needs; the API and the UI decide nothing. A review
is streamed as one JSON event per line so the page can show each step as it finishes.

**Why.** I wanted a UI that explains what the tool does and shows what it is doing,
and Next.js is the other option named in the project brief. A JavaScript front end
cannot call Python, so the logic that lived inside the Streamlit file needed a home
that an HTTP layer could wrap. One service module gives that logic a single place and
a test surface that needs no server.

**Given up.** Two processes and two languages instead of one. The UI's TypeScript
types are written by hand, guarded by a test that compares field names with the Python
models. The API has no authentication and must stay on localhost. The MCP server still
calls `run_review` directly and has not been moved onto the service yet.

## 20. Live prices for all five services, matched by name and unit

**Decision.** The Billing Catalog client reads the SKUs of all five services, in parallel.
A SKU becomes a price only when its description **and** its unit match. Each service falls
back to the static table on its own, and the report names the ones that did. The API key
is sent in a header. Whatever goes wrong behind `load_prices` ends as a fallback.

**Why.** The old boundary caught transport errors only: a 200 answer that was not JSON, or
JSON of another shape, would have ended as a lost cost estimate or a crash. The unit check
is needed because the catalog lists "Nearline Storage Iowa" per GB-month and an
early-delete charge of almost the same name per GB-day. The header is there because HTTP
libraries log URLs and put them in error messages.

**What it showed.** The static table is exact for Compute Engine, disks and the GKE fee.
Its rough region multipliers overprice or underprice Cloud Storage, Cloud Run and Cloud SQL
outside the US (README, "Static prices against live list prices"). The GKE fee is the same
everywhere, so it no longer gets a multiplier.

**Given up.** The first review after a start waits for five catalogs (about 10 s). Cloud
SQL is priced from the MySQL SKUs whatever the engine, and SQL Server licences are not
included. The evals still use the static table, so that results stay comparable.

## 21. A total time limit per step and per review

**Decision.** An LLM step has 90 seconds in total and a review 180, whatever happens inside
(attempts, retries, the JSON repair). At the limit the step stops waiting and uses the same
fallback as any other failure. A review out of time starts no further LLM step.

**Why.** One saved review waited 628 seconds. The 60-second timeout is per attempt, and a
provider that keeps the connection alive never reaches it. 90 s was chosen from the saved
runs: of 420 report-writing steps two took longer (141 s and 628 s), so the limit would
have cut off 2 of 420 and nothing else.

**Alternatives.** Fewer retries (a slow answer is still unlimited); async calls with
cancellation (the client's connection pool is tied to one event loop, and every node and
test is synchronous); a circuit breaker that stops calling a failing provider (useful with
many users at once, not for one local user; the review limit bounds the damage already).

**Given up.** The call runs in its own thread, and Python cannot stop a thread. At the limit
it is left behind and ends on its own; what it still spends is not in the usage count. The
ReAct baseline has no such limit, because it is not part of the product.

## 22. Structured logs with ids, and the cause only in the log

**Decision.** `tools/logs.py` writes one JSON object per line to stderr. Every review has an
id, every HTTP request has one, and the log joins them. A user sees the exception type and
the review id; the log has the chain of causes and where each was raised.

**Why.** Messages were cut down to an exception type so that nothing leaks, and the cause
was then thrown away. A failure in real use could not be diagnosed at all.

**How it stays safe.** Nothing is logged that is not passed in, and Terraform, prompts and
replies never are. Exception messages are redacted (values of secret-looking environment
variables, key patterns, `password is ...`) and cut to 300 characters. Pydantic messages,
which quote the input, are reduced to field and error type. Only this project's logger is
configured. No new dependency.

**Given up.** Redaction is by pattern, so it is a second line of defence, not a proof.

## 23. TypeScript types generated from the Pydantic models

**Decision.** `api/schema.py` writes the OpenAPI schema to `web/lib/openapi.json`, and
`openapi-typescript` turns it into types. A pytest test and `npm run types:check` fail when
either file is out of date. A stream event is a union of three models, not one model with
optional fields. Playwright tests the whole path in a browser with a scripted LLM.

**Why.** The old contract test compared field names. It could not see a wrong type or a
missing null. Generating the types found a real slip on the first compile: the page read
`message` on events that do not have one.

**Given up.** Three development dependencies (`openapi-typescript`, `@playwright/test`,
`@vitest/coverage-v8`), two generated files in the repository, and a step to remember when
a model changes (the tests say which).

## 24. CI on every push, with no secrets

**Decision.** GitHub Actions runs ruff, pytest with coverage, the type checks, lint, web
tests, the build and the browser tests. It has no API key and calls no paid service.

**Why.** The README said the checks pass, and nothing enforced it. Coverage has a floor at
the value measured when it was added, so it can only be lowered on purpose.

**Given up.** The evals with a real model are still run by hand, because they cost money.

## 25. A held-out set labelled by Checkov

**Decision.** 35 Terraform files by other people, chosen by code at pinned commits
(`evals/heldout_select.py`), labelled by Checkov 3.4.7 through a mapping from its checks to
our rules (`evals/heldout_label.py`). A rule is scored only where Checkov has an equivalent
check, and only on the resource types that check looks at. Checkov is not a dependency.

**Why.** Every security score so far was on samples written next to the rules. I cannot
label files independently, because I know the rules. A scanner by other people does not.

**Given up.** A tool is not a person: it has bugs and opinions, and it may share blind spots
with rules that follow the same benchmarks. Five rules have no equivalent and stay unscored.
The mapping is mine; it was fixed from titles alone, before the first scan.
