const hasWindow = typeof window !== "undefined" && typeof window.location !== "undefined";
const pathname = hasWindow ? window.location.pathname : "";
const segments = pathname.split("/").filter(Boolean);

let basePrefix = "";
let repoId = null;

if (segments.length >= 2 && segments[1] === "repos") {
  basePrefix = `/${segments[0]}`;
  repoId = segments[2] || null;
} else if (segments[0] === "repos") {
  repoId = segments[1] || null;
}

export const REPO_ID = repoId;
export const BASE_PATH = repoId ? `${basePrefix}/repos/${repoId}` : basePrefix;
export const HUB_BASE = basePrefix || "/";

let mode = repoId ? "repo" : "unknown";
const hubEndpoint = `${basePrefix || ""}/hub/repos`;

export async function detectContext() {
  if (mode !== "unknown") {
    return { mode, repoId: REPO_ID };
  }
  if (!hasWindow || typeof fetch !== "function") {
    mode = "repo";
    return { mode, repoId: REPO_ID };
  }
  try {
    const res = await fetch(hubEndpoint);
    mode = res.ok ? "hub" : "repo";
  } catch (err) {
    mode = "repo";
  }
  return { mode, repoId: REPO_ID };
}
