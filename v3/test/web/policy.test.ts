import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { testConfig } from "../fakes.ts";
import { policyPath } from "../../src/config/config.ts";
import { buildDeps, mountApp } from "./helpers.ts";

function tempStateDir(): string {
  return mkdtempSync(join(tmpdir(), "car-web-policy-"));
}

describe("web policy", () => {
  test("renders raw policy.toml and reports a clean parse", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir });
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(
      policyPath(config),
      `[classes.reply]\nenabled = true\nmax_per_hour = 10\n`,
    );
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("classes.reply");
    expect(body).toContain(">ok<");
  });

  test("reports a missing policy.toml without throwing", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir });
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("No policy.toml found");
  });

  test("surfaces a parse error for malformed toml", async () => {
    const stateDir = tempStateDir();
    const config = testConfig({ state_dir: stateDir });
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(policyPath(config), `[classes.reply\nenabled = true\n`);
    const deps = buildDeps({ config });
    const app = mountApp(deps);

    const res = await app.request("/ui/policy");
    const body = await res.text();
    expect(body).toContain(">error<");
  });
});
