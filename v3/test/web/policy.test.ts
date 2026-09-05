import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { testConfig } from "../fakes.ts";
import { policyPath } from "../../src/config/config.ts";
import { buildDeps, mountApp, WEB_AUTH_HEADERS, WEB_TEST_TOKEN } from "./helpers.ts";

function tempStateDir(): string {
  return mkdtempSync(join(tmpdir(), "car-web-policy-"));
}

describe("web policy", () => {
  test("renders raw policy.toml and reports a clean parse", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir, http: { private_reads: true, ingest_tokens: { web: WEB_TEST_TOKEN } } });
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(
      policyPath(config),
      `[classes.reply]\nenabled = true\nmax_per_hour = 10\n`,
    );
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy", { headers: WEB_AUTH_HEADERS });
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("classes.reply");
    expect(body).toContain(">ok<");
    expect(body).toContain("Core safety boundary");
    expect(body).toContain("Core authorizes");
  });

  test("reports a missing policy.toml without throwing", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir, http: { private_reads: true, ingest_tokens: { web: WEB_TEST_TOKEN } } });
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy", { headers: WEB_AUTH_HEADERS });
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("No policy.toml found");
  });

  test("surfaces a parse error for malformed toml", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir, http: { private_reads: true, ingest_tokens: { web: WEB_TEST_TOKEN } } });
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(policyPath(config), `[classes.reply\nenabled = true\n`);
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy", { headers: WEB_AUTH_HEADERS });
    const body = await res.text();
    expect(body).toContain(">error<");
  });
});
