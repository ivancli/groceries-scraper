// Runs the test Worker in node with plain fetch standing in for the VPC binding.
import { parseArgs } from "node:util";

import worker from "../src/index.js";

const { values, positionals } = parseArgs({
  options: {
    origin: { type: "string" },
    "terminal-delay-ms": { type: "string", default: "3000" },  // the responder's delay
  },
  allowPositionals: true,
});
if (!values.origin || !process.env.SCRAPER_ORIGIN_TOKEN) {
  console.error(
    "usage: SCRAPER_ORIGIN_TOKEN=… local-proof.mjs --origin <url> [--terminal-delay-ms <ms>] [scenario ...]",
  );
  process.exit(2);
}

const env = {
  PROOF_TOKEN: "local",
  SCRAPER_ORIGIN_TOKEN: process.env.SCRAPER_ORIGIN_TOKEN,
  SCRAPER_API_URL: new URL("/v1/search", values.origin).href,
  TERMINAL_DELAY_MS: values["terminal-delay-ms"],
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
