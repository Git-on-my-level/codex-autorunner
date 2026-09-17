import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

describe("attention CLI startup", () => {
  const invoke = (script: string, cwd: string, args: string[]) => Bun.spawnSync([process.execPath, script, ...args], { cwd, stdout: "pipe", stderr: "pipe" });

  test("runs from an absolute entrypoint outside v3 without importing the web JSX stack", () => {
    const cwd = mkdtempSync(join(tmpdir(), "car-cli-cwd-"));
    try {
      const script = resolve(import.meta.dir, "../../src/cli.ts");
      const result = invoke(script, cwd, ["request", "--help"]);
      expect(result.exitCode).toBe(0);
      expect(new TextDecoder().decode(result.stdout)).toContain("card request doctor");
      expect(new TextDecoder().decode(result.stderr)).not.toContain("react/jsx");
      const schema = Bun.spawnSync([process.execPath, script, "schema"], { cwd, stdout: "pipe", stderr: "pipe" });
      expect(schema.exitCode).toBe(0);
      const document = JSON.parse(new TextDecoder().decode(schema.stdout));
      expect(document.guide.contract).toBe("car.guide.v1");
      expect(document.guide.writing.join(" ")).toContain("never an internal option id");
      expect(document.packet.required).toContain("goal");
      expect(document.packet.required).not.toContain("options");
      expect(document.packet.required).not.toContain("attempts");
      expect(document.packet.required).not.toContain("uncertainty");
      expect(document.instructions).toContain("0–3600 seconds");
      expect(document.cli_file).toContain("not the request envelope");
    } finally {
      rmSync(cwd, { recursive: true, force: true });
    }
  });

  test("rejects unknown, duplicate, and missing option values before client setup", () => {
    const cwd = mkdtempSync(join(tmpdir(), "car-cli-args-"));
    try {
      const script = resolve(import.meta.dir, "../../src/cli.ts");
      const unknown = invoke(script, cwd, ["raise", "--key", "stable", "--bogus", "value"]);
      expect(unknown.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(unknown.stdout))).toMatchObject({ error: "unknown_option" });
      const short = invoke(script, cwd, ["raise", "-f", "packet.json", "--key", "stable"]);
      expect(short.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(short.stdout))).toMatchObject({ error: "unknown_option" });
      const positional = invoke(script, cwd, ["raise", "packet.json", "--key", "stable"]);
      expect(positional.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(positional.stdout))).toMatchObject({ error: "unexpected_argument" });
      const duplicate = invoke(script, cwd, ["raise", "--key", "stable", "--key", "other"]);
      expect(duplicate.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(duplicate.stdout))).toMatchObject({ error: "duplicate_option" });
      const missing = invoke(script, cwd, ["wait", "req_example", "--timeout"]);
      expect(missing.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(missing.stdout))).toMatchObject({ error: "missing_option_value" });
      const noFile = invoke(script, cwd, ["raise", "--key", "stable"]);
      expect(noFile.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(noFile.stdout))).toMatchObject({ error: "missing_option_value" });
      const envelopePath = join(cwd, "envelope.json");
      writeFileSync(envelopePath, JSON.stringify({ contract: "car.request.v1", idempotency_key: "stable", packet: { goal: "g" } }));
      const envelope = invoke(script, cwd, ["raise", "--key", "stable", "--file", envelopePath]);
      expect(envelope.exitCode).toBe(1);
      expect(JSON.parse(new TextDecoder().decode(envelope.stdout))).toMatchObject({ error: "invalid_packet" });
    } finally {
      rmSync(cwd, { recursive: true, force: true });
    }
  });
});
