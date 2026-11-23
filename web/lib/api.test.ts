import { afterEach, describe, expect, it, vi } from "vitest";

import { streamReview } from "./api";
import type { ReviewEvent } from "./types";

function streamOf(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200 });
}

async function collect(events: AsyncIterable<ReviewEvent>): Promise<ReviewEvent[]> {
  const all: ReviewEvent[] = [];
  for await (const event of events) all.push(event);
  return all;
}

afterEach(() => vi.unstubAllGlobals());

describe("streamReview", () => {
  it("yields one event per line, also when a line arrives in two pieces", async () => {
    const fetchMock = vi.fn(async () =>
      streamOf([
        '{"type":"step","step":"parse"}\n{"type":"st',
        'ep","step":"security_check"}\n',
        '{"type":"rejected","message":"no","review_id":"r1"}',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const events = await collect(streamReview({ terraform: "resource {}", project_id: "shop" }));

    expect(events).toEqual([
      { type: "step", step: "parse" },
      { type: "step", step: "security_check" },
      { type: "rejected", message: "no", review_id: "r1" },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/reviews",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ terraform: "resource {}", project_id: "shop" }),
      }),
    );
  });

  it("turns a failed request into an error event the screen can show", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 502 })));

    const events = await collect(streamReview({ terraform: "x", project_id: "shop" }));

    expect(events).toEqual([
      { type: "error", message: expect.stringMatching(/could not reach the review service/i) },
    ]);
  });

  const STEP = '{"type":"step","step":"parse"}\n';

  it("reports a connection that breaks in the middle of a review", async () => {
    let sent = false;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent) controller.error(new TypeError("network error"));
        else controller.enqueue(new TextEncoder().encode(STEP));
        sent = true;
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body)));

    const events = await collect(streamReview({ terraform: "x", project_id: "shop" }));

    expect(events).toEqual([
      { type: "step", step: "parse" },
      { type: "error", message: expect.stringMatching(/connection .* was lost/i) },
    ]);
  });

  it("reports a stream that ends before the outcome", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([STEP])));

    const events = await collect(streamReview({ terraform: "x", project_id: "shop" }));

    expect(events.at(-1)).toEqual({
      type: "error",
      message: expect.stringMatching(/connection .* was lost/i),
    });
  });

  it.each([
    ["not JSON", "<html>502</html>\n"],
    ["half an event", '{"type":"report","rep'],
    ["no object", "null\n"],
    ["an unknown event", '{"type":"surprise"}\n'],
  ])("reports a line it cannot read: %s", async (_name, line) => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([STEP, line])));

    const events = await collect(streamReview({ terraform: "x", project_id: "shop" }));

    expect(events.at(-1)).toEqual({
      type: "error",
      message: expect.stringMatching(/could not read/i),
    });
  });
});
