const hasWindow = typeof window !== "undefined" && typeof window.location !== "undefined";
const pathname = hasWindow ? window.location.pathname || "/" : "/";
const AUTH_TOKEN_KEY = "car_auth_token";

function getAuthToken() {
  let token = null;
  try {
    token = sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch (_err) {
    token = null;
  }
  if (token) {
    return token;
  }
  if (!hasWindow || !window.location.href) {
    return null;
  }
  const url = new URL(window.location.href);
  const urlToken = url.searchParams.get("token");
  if (!urlToken) {
    return null;
  }
  try {
    sessionStorage.setItem(AUTH_TOKEN_KEY, urlToken);
  } catch (_err) {
    // Ignore storage errors; token can still be used for this session.
  }
  url.searchParams.delete("token");
  if (typeof history !== "undefined" && history.replaceState) {
    history.replaceState(null, "", url.toString());
  }
  return urlToken;
}

function normalizeBase(base) {
  if (!base || base === "/") return "";
  let normalized = base.startsWith("/") ? base : `/${base}`;
  while (normalized.endsWith("/") && normalized.length > 1) {
    normalized = normalized.slice(0, -1);
  }
  return normalized === "/" ? "" : normalized;
}

function detectBasePrefix(path) {
  const prefixes = ["/repos/", "/hub/", "/api/", "/static/", "/cat/"];
  let idx = -1;
  for (const prefix of prefixes) {
    const found = path.indexOf(prefix);
    if (found === 0) {
      return "";
    }
    if (found > 0 && (idx === -1 || found < idx)) {
      idx = found;
    }
  }
  if (idx > 0) {
    return normalizeBase(path.slice(0, idx));
  }
  const parts = path.split("/").filter(Boolean);
  if (parts.length) {
    return normalizeBase(`/${parts[0]}`);
  }
  return "";
}

const basePrefix = detectBasePrefix(pathname);

const repoMatch = pathname.match(/\/repos\/([^/]+)/);
const repoId = repoMatch && repoMatch[1] ? repoMatch[1] : null;

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
    const headers = {};
    const token = getAuthToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    const res = await fetch(hubEndpoint, { headers });
    mode = res.ok ? "hub" : "repo";
  } catch (err) {
    mode = "repo";
  }
  return { mode, repoId: REPO_ID };
}
