/** Adapter-owned local VCS identity resolution. Wire repo labels are never trusted. */
import { execFileSync } from "node:child_process";
import { isAbsolute, resolve } from "node:path";
import { realpathSync } from "node:fs";

function git(cwd: string, args: string[]): string | null {
  try {
    return execFileSync("git", ["-C", cwd, ...args], {
      encoding: "utf8",
      timeout: 1_000,
      maxBuffer: 64 * 1024,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || null;
  } catch {
    return null;
  }
}

export function canonicalRemoteIdentity(remote: string): string | null {
  const value = remote.trim();
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "ssh:" || url.protocol === "git:") {
      const path = url.pathname.replace(/^\/+|\/+$/g, "").replace(/\.git$/i, "");
      return path ? `${url.hostname.toLowerCase()}/${path}` : null;
    }
    if (url.protocol === "file:") return `file:${realpathSync(url.pathname)}`;
  } catch {
    // Local filesystem remotes are handled below.
  }
  const scp = /^(?:[^@/]+@)?([^:/]+):(.+)$/.exec(value);
  if (scp && !/^[A-Za-z]:[\\/]/.test(value)) {
    return `${scp[1]!.toLowerCase()}/${scp[2]!.replace(/^\/+|\/+$/g, "").replace(/\.git$/i, "")}`;
  }
  if (isAbsolute(value)) {
    try {
      return `file:${realpathSync(value)}`;
    } catch {
      return null;
    }
  }
  return null;
}

/** Resolve a canonical identity from the host's Git checkout, never from payload repo text. */
export function resolveLocalRepoIdentity(cwd: string | null | undefined): string | null {
  if (!cwd || !isAbsolute(cwd)) return null;
  let canonicalCwd: string;
  try {
    canonicalCwd = realpathSync(resolve(cwd));
  } catch {
    return null;
  }
  const rootRaw = git(canonicalCwd, ["rev-parse", "--show-toplevel"]);
  if (!rootRaw) return null;
  let root: string;
  try {
    root = realpathSync(rootRaw);
  } catch {
    return null;
  }
  const remote = git(root, ["remote", "get-url", "origin"]);
  return (remote && canonicalRemoteIdentity(remote)) || `file:${root}`;
}
