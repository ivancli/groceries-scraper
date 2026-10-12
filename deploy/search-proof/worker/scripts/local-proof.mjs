// Runs the test Worker in node against a proof responder over plain HTTP, standing in for the
// VPC binding: `node scripts/local-proof.mjs --origin http://127.0.0.1:8080 [scenario ...]`.
// Reads the origin credential from SCRAPER_ORIGIN_TOKEN; prints the Worker's JSON report.
import { parseArgs } from "node:util";

import worker from "../src/index.js";

const { values, positionals } = parseArgs({
  options: { origin: { type: "string" } },
  allowPositionals: true,
});
if (!values.origin || !process.env.SCRAPER_ORIGIN_TOKEN) {
  console.error("usage: SCRAPER_ORIGIN_TOKEN=… local-proof.mjs --origin <url> [scenario ...]");
  process.exit(2);
}

const env = {
  PROOF_TOKEN: "local",
  SCRAPER_ORIGIN_TOKEN: process.env.SCRAPER_ORIGIN_TOKEN,
  SCRAPER_API_URL: new URL("/v1/search", values.origin).href,
  SCRAPER_API: { fetch: (url, init) => fetch(url, init) },
};
const body = positionals.length ? { scenarios: positionals } : {};
const response = await worker.fetch(
  new Request("http://proof.local/proof", {
    method: "POST",
    headers: { Authorization: "Bearer local" },
    body: JSON.stringify(body),
  }),
  env,
);
console.log(JSON.stringify(await response.json(), null, 2));
