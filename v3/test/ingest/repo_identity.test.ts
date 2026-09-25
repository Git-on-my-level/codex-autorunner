import { describe, expect, test } from "bun:test";
import { canonicalRemoteIdentity, resolveLocalRepoIdentity } from "../../src/ingest/repo_identity.ts";

describe("adapter-owned repository identity", () => {
  test("canonicalizes supported public Git remote forms", () => {
    expect(canonicalRemoteIdentity("git@github.com:Acme/Repo.git")).toBe("github.com/Acme/Repo");
    expect(canonicalRemoteIdentity("https://github.com/Acme/Repo.git")).toBe("github.com/Acme/Repo");
    expect(canonicalRemoteIdentity("not a remote")).toBeNull();
  });

  test("resolves this checkout from host VCS state and rejects relative cwd", () => {
    expect(resolveLocalRepoIdentity(".")).toBeNull();
    expect(resolveLocalRepoIdentity(process.cwd())).toMatch(/^(?:[^/]+\/|file:)/);
  });
});
