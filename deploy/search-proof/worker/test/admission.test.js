import assert from "node:assert/strict";
import { test } from "node:test";

import { callSearch } from "../src/admission.js";

const REQUEST_ID = "request-1";
const EXECUTION_ID = "execution-1";

const accepted = (overrides = {}) =>
  JSON.stringify({
    contract_version: 1,
    type: "accepted",
    request_id: REQUEST_ID,
    execution_id: EXECUTION_ID,
    adapter_version: "fixture-v1",
    execution_expires_at: "2099-01-01T00:00:10Z",
    joined: false,
    ...overrides,
  }) + "\n";

const result = (overrides = {}) =>
  JSON.stringify({
    contract_version: 1,
    type: "result",
    request_id: REQUEST_ID,
    execution_id: EXECUTION_ID,
    status: "ok",
    source_at: "2099-01-01T00:00:01Z",
    results: [],
    ...overrides,
  }) + "\n";

const pause = (ms) => ({ pause: ms });
// Node timers may fire a millisecond or two early against performance.now().
const JITTER_MS = 5;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A Response whose body arrives piece by piece, with pauses between pieces. */
function streamed(pieces, { status = 200, contentType = "application/x-ndjson" } = {}) {
  const encoder = new TextEncoder();
  const queue = [...pieces];
  const body = new ReadableStream({
    async pull(controller) {
      while (queue.length && queue[0].pause !== undefined) await sleep(queue.shift().pause);
      if (!queue.length) return controller.close();
      const piece = queue.shift();
      controller.enqueue(typeof piece === "string" ? encoder.encode(piece) : piece);
    },
  });
  return new Response(body, { status, headers: { "Content-Type": contentType } });
}

const call = (respond, options = {}) =>
  callSearch({
    fetch: async (url, init) => respond(url, init),
    url: "http://scraper-search:8080/v1/search",
    token: "origin-token",
    request: { request_id: REQUEST_ID, adapter_version: "fixture-v1" },
    admissionMs: 200,
    totalMs: 600,
    ...options,
  });

test("an accepted frame then a delayed result is a success, timed separately", async () => {
  const outcome = await call(() => streamed([accepted(), pause(150), result()]));

  assert.equal(outcome.ok, true);
  assert.equal(outcome.accepted.execution_id, EXECUTION_ID);
  assert.deepEqual(outcome.terminal.results, []);
  assert.ok(outcome.acceptedMs < 100, `accepted after ${outcome.acceptedMs}ms`);
  assert.ok(outcome.terminalMs >= 150 - JITTER_MS, `terminal after ${outcome.terminalMs}ms`);
});

test("posts the request with the origin credential to the fixed URL", async () => {
  let seen;
  await call((url, init) => {
    seen = { url, init };
    return streamed([accepted(), result()]);
  });

  assert.equal(seen.url, "http://scraper-search:8080/v1/search");
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers.Authorization, "Bearer origin-token");
  assert.equal(seen.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(seen.init.body), {
    request_id: REQUEST_ID,
    adapter_version: "fixture-v1",
  });
  assert.ok(seen.init.signal instanceof AbortSignal);
});

const failure = async (pieces, options, responseOptions) => {
  const outcome = await call(() => streamed(pieces, responseOptions), options);
  assert.equal(outcome.ok, false, JSON.stringify(outcome));
  return outcome.failure;
};

test("frames split mid-token and mid-UTF-8 character still parse", async () => {
  const bytes = new TextEncoder().encode(accepted() + result({ status: "ok", note: "café ✓" }));
  const pieces = [];
  for (let i = 0; i < bytes.length; i += 3) pieces.push(bytes.slice(i, i + 3), pause(1));

  const outcome = await call(() => streamed(pieces));

  assert.equal(outcome.ok, true, JSON.stringify(outcome));
  assert.equal(outcome.terminal.note, "café ✓");
});

test("both frames coalesced into one chunk still parse", async () => {
  const outcome = await call(() => streamed([accepted() + result()]));

  assert.equal(outcome.ok, true);
});

test("a post-admission error frame is a terminal frame, not a stream failure", async () => {
  const error = JSON.stringify({
    contract_version: 1,
    type: "error",
    request_id: REQUEST_ID,
    execution_id: EXECUTION_ID,
    code: "deadline_exceeded",
  });

  const outcome = await call(() => streamed([accepted(), error + "\n"]));

  assert.equal(outcome.ok, true);
  assert.equal(outcome.terminal.code, "deadline_exceeded");
});

for (const [name, pieces, expected] of [
  ["an accepted frame over 1 KiB", [accepted({ pad: "x".repeat(1024) }), result()], "oversize_frame"],
  ["an unterminated accepted frame over 1 KiB", ['{"pad":"' + "x".repeat(1100)], "oversize_frame"],
  ["a terminal frame over 64 KiB", [accepted(), result({ pad: "x".repeat(65536) })], "oversize_frame"],
  ["a terminal frame for another execution", [accepted(), result({ execution_id: "other" })], "mismatched_frame"],
  ["a terminal frame for another request", [accepted(), result({ request_id: "other" })], "mismatched_frame"],
  ["an acceptance for another request", [accepted({ request_id: "other" }), result()], "mismatched_frame"],
  ["an acceptance from another adapter", [accepted({ adapter_version: "x" }), result()], "invalid_frame"],
  ["an acceptance of another contract version", [accepted({ contract_version: 2 })], "invalid_frame"],
  ["an acceptance without a joined flag", [accepted({ joined: undefined }), result()], "invalid_frame"],
  ["a result before any acceptance", [result()], "missing_acceptance"],
  ["a second acceptance", [accepted(), accepted(), result()], "duplicate_acceptance"],
  ["a result without results", [accepted(), result({ results: undefined })], "invalid_frame"],
  ["a frame that is not JSON", [accepted(), "nope\n"], "invalid_frame"],
  ["a frame that is not an object", [accepted(), "[]\n"], "invalid_frame"],
  ["a frame that is not UTF-8", [accepted(), new Uint8Array([0xff, 0x0a])], "invalid_frame"],
  ["EOF before any frame", [], "premature_eof"],
  ["EOF after acceptance", [accepted()], "premature_eof"],
  ["EOF inside the terminal frame", [accepted(), result().slice(0, 20)], "premature_eof"],
  ["data after the terminal frame", [accepted(), result(), "{}"], "trailing_data"],
]) {
  test(`${name} is a bounded ${expected} failure`, async () => {
    assert.equal(await failure(pieces), expected);
  });
}

test("a whole stream over 66 KiB fails even if every frame fits", async () => {
  const terminal = result({ pad: "x".repeat(65536 - 300) });
  const padded = accepted({ pad: "y".repeat(1024 - accepted().length - 10) });

  assert.equal(await failure([padded, terminal, " ".repeat(2048)]), "oversize_stream");
});

test("an acceptance later than the admission budget fails without waiting for it", async () => {
  const outcome = await call(() => streamed([pause(400), accepted(), result()]));

  assert.equal(outcome.failure, "admission_timeout");
  assert.ok(outcome.failedMs >= 200 - JITTER_MS && outcome.failedMs < 350, `failed after ${outcome.failedMs}ms`);
});

test("status and headers alone do not count as admission", async () => {
  // Headers arrive at once; the body's first byte never does within the budget.
  const outcome = await call(() => streamed([pause(1000), accepted()]));

  assert.equal(outcome.failure, "admission_timeout");
});

test("an origin that never answers fails at the admission budget and is aborted", async () => {
  let signal;
  const outcome = await call((url, init) => {
    signal = init.signal;
    return new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason)));
  });

  assert.equal(outcome.failure, "admission_timeout");
  assert.equal(signal.aborted, true);
});

test("a terminal frame later than the total budget fails at the deadline", async () => {
  let signal;
  const outcome = await call((url, init) => {
    signal = init.signal;
    return streamed([accepted(), pause(2000), result()]);
  });

  assert.equal(outcome.failure, "deadline_exceeded");
  assert.ok(outcome.acceptedMs < 100);
  assert.ok(outcome.failedMs >= 600 - JITTER_MS && outcome.failedMs < 750, `failed after ${outcome.failedMs}ms`);
  assert.equal(signal.aborted, true, "the caller hangs up so the origin can free its slot");
});

test("a failed stream hangs up on the origin", async () => {
  let signal;
  await call((url, init) => {
    signal = init.signal;
    return streamed([accepted(), result({ execution_id: "other" })]);
  });

  assert.equal(signal.aborted, true);
});

test("a JSON rejection before admission reports the origin's stable code", async () => {
  const body = JSON.stringify({ contract_version: 1, type: "rejected", code: "busy" });

  const outcome = await call(() => streamed([body], { status: 409, contentType: "application/json" }));

  assert.deepEqual(
    { ok: outcome.ok, failure: outcome.failure, status: outcome.status, code: outcome.code },
    { ok: false, failure: "rejected", status: 409, code: "busy" },
  );
});

for (const [name, pieces, options, expected] of [
  ["a rejection over 1 KiB", ['{"code":"' + "x".repeat(1100) + '"}'], { status: 401, contentType: "application/json" }, "invalid_rejection"],
  ["a rejection that is not JSON", ["nope"], { status: 401, contentType: "application/json" }, "invalid_rejection"],
  ["a non-JSON error status", ["Bad gateway"], { status: 502, contentType: "text/html" }, "bad_status"],
  ["a stream that is not NDJSON", [accepted(), result()], { contentType: "text/plain" }, "bad_content_type"],
]) {
  test(`${name} is a bounded ${expected} failure`, async () => {
    assert.equal(await failure(pieces, undefined, options), expected);
  });
}

test("an unreachable origin is a bounded failure", async () => {
  const outcome = await call(async () => {
    throw new TypeError("fetch failed");
  });

  assert.equal(outcome.failure, "unreachable");
});

test("a body that breaks off mid-stream is a bounded disconnected failure", async () => {
  const encoder = new TextEncoder();
  let sent = false;
  const body = new ReadableStream({
    pull(controller) {
      if (sent) return controller.error(new TypeError("terminated"));
      sent = true;
      controller.enqueue(encoder.encode(accepted()));
    },
  });

  const outcome = await call(
    () => new Response(body, { headers: { "Content-Type": "application/x-ndjson" } }),
  );

  assert.equal(outcome.failure, "disconnected");
});
