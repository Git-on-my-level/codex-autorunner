/**
 * The default Bun.spawn runner. Exercised against plain OS utilities only —
 * no vendor CLI is ever spawned by the suite.
 */
import { describe, expect, test } from "bun:test";
import {
  createBunRunner,
  EXIT_SPAWN_FAILED,
  EXIT_TIMEOUT,
  truncateOutput,
} from "../../src/actions/runner.ts";

const run = createBunRunner();

describe("createBunRunner", () => {
  test("captures stdout and a zero exit", async () => {
    const res = await run(["/bin/echo", "hello world"], { timeoutMs: 5000 });
    expect(res.code).toBe(0);
    expect(res.stdout.trim()).toBe("hello world");
    expect(res.stderr).toBe("");
  });

  test("passes argv elements through verbatim — no shell interpretation", async () => {
    const res = await run(["/bin/echo", "$(id); rm -rf ~"], { timeoutMs: 5000 });
    expect(res.stdout.trim()).toBe("$(id); rm -rf ~");
  });

  test("reports a nonzero exit code", async () => {
    const res = await run(["/bin/sh", "-c", "exit 3"], { timeoutMs: 5000 });
    expect(res.code).toBe(3);
  });

  test("honours cwd", async () => {
    const res = await run(["/bin/pwd"], { cwd: "/tmp", timeoutMs: 5000 });
    expect(res.stdout.trim()).toContain("tmp");
  });

  test("feeds stdin", async () => {
    const res = await run(["/bin/cat"], { stdin: "piped input", timeoutMs: 5000 });
    expect(res.stdout).toBe("piped input");
  });

  test("kills on timeout and reports it", async () => {
    const res = await run(["/bin/sleep", "10"], { timeoutMs: 150 });
    expect(res.code).toBe(EXIT_TIMEOUT);
    expect(res.stderr).toContain("killed after 150ms");
  });

  test("a missing executable is a nonzero code, never a throw", async () => {
    const res = await run(["/nonexistent/car-not-a-binary"], { timeoutMs: 5000 });
    expect(res.code).toBe(EXIT_SPAWN_FAILED);
    expect(res.stdout).toBe("");
  });

  test("an empty argv is refused", async () => {
    const res = await run([], { timeoutMs: 5000 });
    expect(res.code).toBe(EXIT_SPAWN_FAILED);
  });

  test("output is bounded", () => {
    const truncated = truncateOutput("x".repeat(100), 10);
    expect(truncated.startsWith("x".repeat(10))).toBe(true);
    expect(truncated).toContain("truncated 90 chars");
    expect(truncateOutput("short", 10)).toBe("short");
  });
});
