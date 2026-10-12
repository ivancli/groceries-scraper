// The isolated test Worker: drives fixture scenarios through the VPC Service binding to the
// proof responder and reports admission/terminal timings. It never touches the tracker or a retailer.
import { callSearch } from "./admission.js";

export const ADMISSION_MS = 2_000;
export const TOTAL_MS = 10_000;

// What a correct route and reader must make of each fixture stream.
export const EXPECTED = {
  ok: { ok: true },
  fragmented: { ok: true },
  oversize: { ok: false, failure: "oversize_frame" },
  mismatched: { ok: false, failure: "mismatched_frame" },
  duplicate: { ok: false, failure: "duplicate_acceptance" },
  premature: { ok: false, failure: "premature_eof" },
  late: { ok: false, failure: "admission_timeout" },
};

export default {
  async fetch(request, env) {
    if (!env.PROOF_TOKEN || !env.SCRAPER_ORIGIN_TOKEN) {
      return Response.json({ error: "unconfigured" }, { status: 503 });
    }
    const supplied = request.headers.get("Authorization") ?? "";
    if (!sameSecret(supplied, `Bearer ${env.PROOF_TOKEN}`)) {
      return Response.json({ error: "unauthorized" }, { status: 401 });
    }
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/proof") {
      return Response.json({ error: "not_found" }, { status: 404 });
    }
    let scenarios;
    try {
      ({ scenarios = Object.keys(EXPECTED) } = await request.json());
    } catch {
      return Response.json({ error: "invalid_request" }, { status: 400 });
    }
    if (!Array.isArray(scenarios) || !scenarios.every((name) => Object.hasOwn(EXPECTED, name))) {
      return Response.json({ error: "invalid_request", known: Object.keys(EXPECTED) }, { status: 400 });
    }
    return Response.json(await prove(env, scenarios));
  },
};

/** One search at a time: the origin admits a single execution, so overlap would only be Busy. */
export async function prove(env, scenarios) {
  const runs = [];
  for (const scenario of scenarios) {
    const request = searchRequest(scenario);
    const outcome = await callSearch({
      fetch: (url, init) => env.SCRAPER_API.fetch(url, init),
      url: env.SCRAPER_API_URL,
      token: env.SCRAPER_ORIGIN_TOKEN,
      request,
      admissionMs: ADMISSION_MS,
      totalMs: TOTAL_MS,
    });
    const expected = EXPECTED[scenario];
    const passed =
      outcome.ok === expected.ok &&
      outcome.failure === expected.failure &&
      (!outcome.ok || outcome.acceptedMs < ADMISSION_MS);
    const { accepted, terminal, ...timings } = outcome;
    runs.push({ scenario, request_id: request.request_id, passed, ...timings });
  }
  return { passed: runs.every((run) => run.passed), runs };
}

function searchRequest(scenario) {
  return {
    contract_version: 1,
    request_id: crypto.randomUUID(),
    execution_key: "search-proof",
    retailer: "fixture",
    adapter_version: "fixture-v1",
    query: scenario,
    context: {},
    budget_ms: TOTAL_MS,
    admission_budget_ms: ADMISSION_MS,
    expires_at: new Date(Date.now() + TOTAL_MS).toISOString(),
  };
}

function sameSecret(a, b) {
  const [x, y] = [new TextEncoder().encode(a), new TextEncoder().encode(b)];
  if (x.length !== y.length) return false;
  let difference = 0;
  for (let i = 0; i < x.length; i++) difference |= x[i] ^ y[i];
  return difference === 0;
}
