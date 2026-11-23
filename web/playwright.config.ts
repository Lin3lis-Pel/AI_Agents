// End-to-end tests: a real browser, the real Next.js server and the real Python API with a
// scripted LLM (tests/e2e_server.py). Build first: `npm run build && npm run test:e2e`.
//
// The API's port is fixed (the rewrite in next.config.ts points at it), so the real API
// must not be running: the tests refuse to start rather than use it.

import { defineConfig } from "@playwright/test";

const PORT = 3100;

export default defineConfig({
  testDir: "./e2e",
  workers: 1, // the tests share one server and its memory
  timeout: 60_000,
  expect: { timeout: 15_000 },
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    // Set PLAYWRIGHT_CHANNEL=msedge (or chrome) to use an installed browser.
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  },
  webServer: [
    {
      command: "uv run python -m tests.e2e_server",
      cwd: "..",
      url: "http://127.0.0.1:8000/api/capabilities",
      reuseExistingServer: false,
      timeout: 180_000, // the first run downloads the embedding model
    },
    {
      command: `npx next start --port ${PORT}`,
      url: `http://localhost:${PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
