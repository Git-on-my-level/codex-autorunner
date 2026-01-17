const hasWindow = typeof window !== "undefined" && typeof window.location !== "undefined";
const pathname = hasWindow ? window.location.pathname || "/" : "/";
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

const basePrefix =
  hasWindow && typeof window.__CAR_BASE_PREFIX !== "undefined"
    ? window.__CAR_BASE_PREFIX
    : detectBasePrefix(pathname);

const repoId =
  hasWindow && typeof window.__CAR_REPO_ID !== "undefined"
    ? window.__CAR_REPO_ID
    : (() => {
        const match = pathname.match(/\/repos\/([^/]+)/);
        return match && match[1] ? match[1] : null;
      })();

const derivedBasePath = repoId ? `${basePrefix}/repos/${repoId}` : basePrefix;
const basePath =
  hasWindow && typeof window.__CAR_BASE_PATH !== "undefined"
    ? window.__CAR_BASE_PATH
    : derivedBasePath;

export const REPO_ID = repoId;
export const BASE_PATH = basePath;
export const HUB_BASE = basePrefix || "/";
