const hasWindow = typeof window !== "undefined" && typeof window.location !== "undefined";
const pathname = hasWindow ? window.location.pathname || "/" : "/";
const AUTH_TOKEN_KEY = "car_auth_token";

function getAuthToken(): string | null {
  let token: string | null = null;
  try {
    token = sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch (_err) {
    token = null;
  }
  if (token) {
    return token;
  }
  if (hasWindow && (window as unknown as { __CAR_AUTH_TOKEN?: string }).__CAR_AUTH_TOKEN) {
    return (window as unknown as { __CAR_AUTH_TOKEN: string }).__CAR_AUTH_TOKEN;
  }
  return null;
}

function normalizeBase(base: string): string {
  if (!base || base === "/") return "";
  let normalized = base.startsWith("/") ? base : `/${base}`;
  while (normalized.endsWith("/") && normalized.length > 1) {
    normalized = normalized.slice(0, -1);
  }
  return normalized === "/" ? "" : normalized;
}

function detectBasePrefix(path: string): string {
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

const basePrefix =
  hasWindow && typeof (window as unknown as { __CAR_BASE_PREFIX?: string }).__CAR_BASE_PREFIX !== "undefined"
    ? (window as unknown as { __CAR_BASE_PREFIX: string }).__CAR_BASE_PREFIX
    : detectBasePrefix(pathname);

const repoId =
  hasWindow && typeof (window as unknown as { __CAR_REPO_ID?: string }).__CAR_REPO_ID !== "undefined"
    ? (window as unknown as { __CAR_REPO_ID: string }).__CAR_REPO_ID
    : (() => {
        const match = pathname.match(/\/repos\/([^/]+)/);
        return match && match[1] ? match[1] : null;
      })();

const derivedBasePath = repoId ? `${basePrefix}/repos/${repoId}` : basePrefix;
const basePath =
  hasWindow && typeof (window as unknown as { __CAR_BASE_PATH?: string }).__CAR_BASE_PATH !== "undefined"
    ? (window as unknown as { __CAR_BASE_PATH: string }).__CAR_BASE_PATH
    : derivedBasePath;

export const REPO_ID: string | null = repoId;
export const BASE_PATH: string = basePath;
export const HUB_BASE: string = basePrefix || "/";

let mode: string = repoId ? "repo" : "unknown";
const hubEndpoint = `${basePrefix || ""}/hub/repos`;

interface DetectContextResult {
  mode: string;
  repoId: string | null;
}

export async function detectContext(): Promise<DetectContextResult> {
  if (mode !== "unknown") {
    return { mode, repoId: REPO_ID };
  }
  if (!hasWindow || typeof fetch !== "function") {
    mode = "repo";
    return { mode, repoId: REPO_ID };
  }
  try {
    const headers: Record<string, string> = {};
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
