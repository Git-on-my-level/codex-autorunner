/**
 * Subprocess seam. Everything in src/actions/ that touches a vendor CLI goes
 * through a `Runner`, so tests inject a fake and never spawn a real process.
 *
 * argv is ALWAYS an array — no shell strings anywhere in this layer.
 */

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface RunOpts {
  cwd?: string;
  stdin?: string;
  timeoutMs: number;
}

export type Runner = (argv: string[], opts: RunOpts) => Promise<RunResult>;

/** Captured output is bounded; adapters and audit rows never hold unbounded blobs. */
export const MAX_CAPTURE_BYTES = 64 * 1024;

/** Exit code used when the process was killed for exceeding its timeout. */
export const EXIT_TIMEOUT = 124;
/** Exit code used when the executable could not be spawned at all. */
export const EXIT_SPAWN_FAILED = 127;

export function truncateOutput(text: string, max = MAX_CAPTURE_BYTES): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}\n…[truncated ${text.length - max} chars]`;
}

/**
 * Default runner: Bun.spawn, argv-array exec, hard kill on timeout.
 * Never throws — a spawn failure surfaces as a nonzero code so every caller
 * can use one code path for "the CLI did not work".
 */
export function createBunRunner(): Runner {
  return async (argv, opts) => {
    const [cmd, ...rest] = argv;
    if (!cmd) return { code: EXIT_SPAWN_FAILED, stdout: "", stderr: "runner: empty argv" };
    let proc: ReturnType<typeof Bun.spawn>;
    try {
      proc = Bun.spawn([cmd, ...rest], {
        cwd: opts.cwd,
        stdin: opts.stdin === undefined ? "ignore" : new TextEncoder().encode(opts.stdin),
        stdout: "pipe",
        stderr: "pipe",
      });
    } catch (err) {
      return { code: EXIT_SPAWN_FAILED, stdout: "", stderr: `runner: spawn failed: ${String(err)}` };
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        proc.kill(9);
      } catch {
        /* already gone */
      }
    }, opts.timeoutMs);
    try {
      const [stdout, stderr] = await Promise.all([
        new Response(proc.stdout as ReadableStream).text(),
        new Response(proc.stderr as ReadableStream).text(),
      ]);
      const code = await proc.exited;
      return {
        code: timedOut ? EXIT_TIMEOUT : code,
        stdout: truncateOutput(stdout),
        stderr: truncateOutput(timedOut ? `${stderr}\n[car] killed after ${opts.timeoutMs}ms` : stderr),
      };
    } catch (err) {
      return { code: EXIT_SPAWN_FAILED, stdout: "", stderr: `runner: ${String(err)}` };
    } finally {
      clearTimeout(timer);
    }
  };
}
