// The Worker side of search v1 admission: one accepted frame within the admission budget,
// then one matching terminal frame, then EOF — read incrementally, never with .json()/.text().

export const CONTRACT_VERSION = 1;
const NEWLINE = 0x0a;
const KIB = 1024;
export const LIMITS = { accepted: KIB, terminal: 64 * KIB, stream: 66 * KIB, rejection: KIB };

class SearchFailure extends Error {
  constructor(failure) {
    super(failure);
    this.failure = failure;
  }
}

export async function callSearch({ fetch, url, token, request, admissionMs, totalMs }) {
  const started = performance.now();
  const elapsed = () => Math.round(performance.now() - started);
  const controller = new AbortController();
  // Every wait races these, so a silent origin or stalled body can't outlast a budget.
  let expire;
  const expired = new Promise((_, reject) => (expire = (failure) => reject(new SearchFailure(failure))));
  expired.catch(() => {});
  const admissionTimer = setTimeout(() => expire("admission_timeout"), admissionMs);
  const totalTimer = setTimeout(() => expire("deadline_exceeded"), totalMs);
  const bounded = (promise) => Promise.race([promise, expired]);
  let reader;
  let acceptedMs;
  try {
    let response;
    try {
      response = await bounded(
        fetch(url, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(request),
          signal: controller.signal,
        }),
      );
    } catch (error) {
      throw error instanceof SearchFailure ? error : new SearchFailure("unreachable");
    }
    reader = response.body.getReader();
    const read = () =>
      bounded(
        reader.read().catch(() => {
          throw new SearchFailure("disconnected");
        }),
      );
    if (response.status !== 200) return await rejection(response, read);
    if (mediaType(response) !== "application/x-ndjson") throw new SearchFailure("bad_content_type");
    const frames = readFrames(read);

    const accepted = await frames.next(LIMITS.accepted);
    checkAccepted(accepted, request);
    clearTimeout(admissionTimer);
    acceptedMs = elapsed();

    const terminal = await frames.next(LIMITS.terminal);
    checkTerminal(terminal, accepted);
    await frames.end();
    return { ok: true, accepted, terminal, acceptedMs, terminalMs: elapsed() };
  } catch (error) {
    if (!(error instanceof SearchFailure)) throw error;
    // Hang up, so the origin sees the disconnect and frees its slot rather than finishing.
    controller.abort();
    reader?.cancel().catch(() => {});
    return { ok: false, failure: error.failure, acceptedMs, failedMs: elapsed() };
  } finally {
    clearTimeout(admissionTimer);
    clearTimeout(totalTimer);
  }
}

const mediaType = (response) =>
  (response.headers.get("Content-Type") ?? "").split(";")[0].trim().toLowerCase();

/** A pre-admission JSON rejection: nothing executed, and the origin names why. */
async function rejection(response, read) {
  if (mediaType(response) !== "application/json") throw new SearchFailure("bad_status");
  const body = [];
  let size = 0;
  for (let chunk = await read(); !chunk.done; chunk = await read()) {
    size += chunk.value.length;
    if (size > LIMITS.rejection) throw new SearchFailure("invalid_rejection");
    body.push(chunk.value);
  }
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(concat(...body)));
  } catch {
    throw new SearchFailure("invalid_rejection");
  }
  if (!nonEmptyString(parsed?.code)) throw new SearchFailure("invalid_rejection");
  return { ok: false, failure: "rejected", status: response.status, code: parsed.code };
}

function checkAccepted(frame, request) {
  if (frame.type === "result" || frame.type === "error") throw new SearchFailure("missing_acceptance");
  if (
    frame.type !== "accepted" ||
    frame.contract_version !== CONTRACT_VERSION ||
    frame.adapter_version !== request.adapter_version ||
    !nonEmptyString(frame.execution_id) ||
    !nonEmptyString(frame.execution_expires_at) ||
    typeof frame.joined !== "boolean"
  ) {
    throw new SearchFailure("invalid_frame");
  }
  if (frame.request_id !== request.request_id) throw new SearchFailure("mismatched_frame");
}

function checkTerminal(frame, accepted) {
  if (frame.type === "accepted") throw new SearchFailure("duplicate_acceptance");
  const wellFormed =
    frame.contract_version === CONTRACT_VERSION &&
    ((frame.type === "result" && nonEmptyString(frame.status) && Array.isArray(frame.results)) ||
      (frame.type === "error" && nonEmptyString(frame.code)));
  if (!wellFormed) throw new SearchFailure("invalid_frame");
  if (frame.request_id !== accepted.request_id || frame.execution_id !== accepted.execution_id) {
    throw new SearchFailure("mismatched_frame");
  }
}

const nonEmptyString = (value) => typeof value === "string" && value.length > 0;

/** Splits the body into JSON frames by bytes, so limits hold before decoding or parsing. */
function readFrames(read) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffered = new Uint8Array(0);
  let total = 0;
  let done = false;

  const pull = async () => {
    const chunk = await read();
    if (chunk.done) {
      done = true;
      return;
    }
    total += chunk.value.length;
    if (total > LIMITS.stream) throw new SearchFailure("oversize_stream");
    buffered = concat(buffered, chunk.value);
  };

  return {
    async next(limit) {
      for (;;) {
        const newline = buffered.indexOf(NEWLINE);
        if (newline > limit || (newline === -1 && buffered.length > limit)) {
          throw new SearchFailure("oversize_frame");
        }
        if (newline !== -1) {
          const line = buffered.subarray(0, newline);
          buffered = buffered.slice(newline + 1);
          return parse(decoder, line);
        }
        if (done) throw new SearchFailure("premature_eof");
        await pull();
      }
    },
    async end() {
      while (!done && buffered.length === 0) await pull();
      if (buffered.length) throw new SearchFailure("trailing_data");
    },
  };
}

function parse(decoder, line) {
  let frame;
  try {
    frame = JSON.parse(decoder.decode(line));
  } catch {
    throw new SearchFailure("invalid_frame");
  }
  if (frame === null || typeof frame !== "object" || Array.isArray(frame)) {
    throw new SearchFailure("invalid_frame");
  }
  return frame;
}

function concat(...parts) {
  const joined = new Uint8Array(parts.reduce((size, part) => size + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}
