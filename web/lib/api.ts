// The only place the UI talks to the Python API. Paths start with /api and are forwarded
// to the Python server by the rewrite in next.config.ts.

import type {
  Capabilities,
  FeedbackChoice,
  FinishResult,
  ProjectMemory,
  ReviewEvent,
  ReviewRequest,
  SampleFile,
  SampleList,
} from "./types";

const UNREACHABLE =
  "Could not reach the review service. Check that the Python API is running " +
  "(uv run python -m api.server) and try again.";
const INTERRUPTED =
  "The connection to the review service was lost before the report arrived. " +
  "Please run the review again.";
const UNREADABLE =
  "The review service sent an answer this page could not read. Please run the review again.";
const EVENT_TYPES = ["step", "report", "rejected", "error"];

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return (await response.json()) as T;
}

async function sendJson<T>(path: string, method: "POST" | "PUT", body: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return (await response.json()) as T;
}

export const getCapabilities = () => getJson<Capabilities>("/api/capabilities");

export const getSamples = async () => (await getJson<SampleList>("/api/samples")).samples;

export const getSample = async (name: string) =>
  (await getJson<SampleFile>(`/api/samples/${name}`)).terraform;

export const getMemory = (project: string) =>
  getJson<ProjectMemory>(`/api/projects/${encodeURIComponent(project)}/memory`);

export const saveMemory = (project: string, memory: ProjectMemory) =>
  sendJson<ProjectMemory>(`/api/projects/${encodeURIComponent(project)}/memory`, "PUT", memory);

export const sendFeedback = (reviewId: string, feedback: FeedbackChoice[]) =>
  sendJson<FinishResult>(`/api/reviews/${encodeURIComponent(reviewId)}/feedback`, "POST", {
    feedback,
  });

/**
 * Run a review. The API answers with one JSON event per line, sent as each step finishes,
 * so this yields events while the review is still running.
 *
 * It never throws, and its last event is always the outcome: a report, or a problem. A
 * request that fails, a connection that breaks, a line that is not JSON and a stream that
 * ends too early all become one `error` event, so the screen has a single way to show
 * problems and cannot be left waiting.
 */
export async function* streamReview(request: ReviewRequest): AsyncGenerator<ReviewEvent> {
  let response: Response;
  try {
    response = await fetch("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
  } catch {
    yield problem(UNREACHABLE);
    return;
  }
  if (!response.ok || !response.body) {
    yield problem(
      response.status === 422
        ? "The request was not accepted. Check the project name and the size of the file."
        : UNREACHABLE,
    );
    return;
  }
  try {
    for await (const line of readLines(response.body)) {
      const event = parseEvent(line);
      yield event;
      if (event.type !== "step") return;
    }
    yield problem(INTERRUPTED); // the stream ended, but the outcome never came
  } catch (error) {
    yield problem(error instanceof UnreadableEvent ? UNREADABLE : INTERRUPTED);
  }
}

class UnreadableEvent extends Error {}

const problem = (message: string): ReviewEvent => ({ type: "error", message });

function parseEvent(line: string): ReviewEvent {
  let event: unknown;
  try {
    event = JSON.parse(line);
  } catch {
    throw new UnreadableEvent();
  }
  const type = (event as { type?: unknown } | null)?.type;
  if (typeof type !== "string" || !EVENT_TYPES.includes(type)) throw new UnreadableEvent();
  return event as ReviewEvent;
}

/** The non-empty lines of a response body. A line may arrive in several pieces. */
async function* readLines(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      const lines = pending.split("\n");
      pending = lines.pop() ?? ""; // the last piece may be half a line
      for (const line of lines) if (line.trim()) yield line;
      if (done) break;
    }
    if (pending.trim()) yield pending;
  } finally {
    // Also when the caller stops early. A broken stream may refuse: nothing left to free.
    reader.cancel().catch(() => {});
  }
}
