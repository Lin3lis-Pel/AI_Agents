# Web UI

The web UI of the GCP Infra Review Agent: Next.js, React and TypeScript. The main
write-up is the [README in the repository root](../README.md).

## Run

The UI needs the Python HTTP API. Start it first, from the repository root:

```bash
uv run python -m api.server        # http://127.0.0.1:8000
```

Then, in this folder:

```bash
npm install
npm run build && npm run start     # http://localhost:3000
npm run dev                        # or: development mode, reloads on change
```

Requests to `/api/*` are forwarded to the Python API by the rewrite in
`next.config.ts`. Set `REVIEW_API_URL` if the API runs somewhere else.

## Check

```bash
npm test              # component tests (Vitest + React Testing Library), no servers needed
npm run lint
npm run typecheck
npm run types:check   # the generated types match lib/openapi.json
npm run build && npm run test:e2e   # browser tests; the real API must not be running
```

## Layout

```
app/            page, layout and styles
components/     Intro, ReviewApp, Progress, Report, FeedbackForm, ProjectMemory,
                AdvancedSettings, each with its test next to it
lib/api.ts      the only place that calls the Python API
lib/openapi.json, lib/api-schema.d.ts
                generated from the Python models: `uv run python -m api.schema` in the
                repository root, then `npm run types`. Do not edit
lib/types.ts    short names for the generated types
e2e/            browser tests (Playwright)
lib/labels.ts   plain-language names for services, steps and severities
```

The component tests fake only the network call. The browser tests fake only the LLM
(`tests/e2e_server.py`). `tests/api/test_contract.py` in the Python project fails when the
saved schema no longer matches the models.
