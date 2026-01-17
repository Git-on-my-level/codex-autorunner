(() => {
  const AUTH_TOKEN_KEY = "car_auth_token";
  const url = new URL(window.location.href);
  const params = url.searchParams;
  let token = params.get("token");
  if (token) {
    window.__CAR_AUTH_TOKEN = token;
    try {
      sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    } catch (_err) {
      // Ignore storage errors; token can still be used for this load.
    }
    params.delete("token");
    if (typeof history !== "undefined" && history.replaceState) {
      history.replaceState(null, "", url.toString());
    }
  } else {
    try {
      token = sessionStorage.getItem(AUTH_TOKEN_KEY);
      if (token) window.__CAR_AUTH_TOKEN = token;
    } catch (_err) {
      // Ignore storage errors; token may still be in memory.
    }
  }

  const normalizeBase = (base) => {
    if (!base || base === "/") return "";
    let normalized = base.startsWith("/") ? base : `/${base}`;
    while (normalized.endsWith("/") && normalized.length > 1) {
      normalized = normalized.slice(0, -1);
    }
    return normalized === "/" ? "" : normalized;
  };
  const detectBasePrefix = (path) => {
    const prefixes = ["/repos/", "/hub/", "/api/", "/static/", "/cat/"];
    let idx = -1;
    for (const prefix of prefixes) {
      const found = path.indexOf(prefix);
      if (found === 0) return "";
      if (found > 0 && (idx === -1 || found < idx)) idx = found;
    }
    if (idx > 0) return normalizeBase(path.slice(0, idx));
    const parts = path.split("/").filter(Boolean);
    if (parts.length) return normalizeBase(`/${parts[0]}`);
    return "";
  };

  const pathname = window.location.pathname || "/";
  const basePrefix = detectBasePrefix(pathname);
  const repoMatch = pathname.match(/\/repos\/([^/]+)/);
  const repoId = repoMatch && repoMatch[1] ? repoMatch[1] : null;

  window.__CAR_BASE_PREFIX = basePrefix || "";
  window.__CAR_REPO_ID = repoId;
  window.__CAR_BASE_PATH = repoId ? `${basePrefix}/repos/${repoId}` : basePrefix;
  window.__AUTH_TOKEN_PRESENT = Boolean(window.__CAR_AUTH_TOKEN);

  const base = basePrefix || "/";
  const baseTag = document.createElement("base");
  baseTag.href = base.endsWith("/") ? base : `${base}/`;
  document.head.appendChild(baseTag);

  const readAssetVersion = () => {
    const queryVersion = new URLSearchParams(window.location.search).get("v");
    if (queryVersion) return queryVersion;
    const bootstrapScript =
      document.currentScript ||
      document.querySelector('script[data-car-bootstrap]');
    if (bootstrapScript && bootstrapScript.src) {
      try {
        const scriptUrl = new URL(bootstrapScript.src, window.location.href);
        return scriptUrl.searchParams.get("v") || "";
      } catch (_err) {
        return "";
      }
    }
    return "";
  };

  const version = readAssetVersion();
  const suffix = version ? `?v=${encodeURIComponent(version)}` : "";
  window.__assetSuffix = suffix;

  const addStylesheet = (href) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = `${href}${suffix}`;
    document.head.appendChild(link);
  };

  addStylesheet("static/styles.css");
  addStylesheet("static/vendor/xterm.css");

  const versionPath = repoId
    ? `${basePrefix || ""}/repos/${repoId}/api/version`
    : `${basePrefix || ""}/hub/version`;
  const normalizedVersionPath = versionPath.startsWith("/")
    ? versionPath
    : `/${versionPath}`;

  const checkVersion = () => {
    fetch(normalizedVersionPath, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const assetVersion = data && data.asset_version;
        if (assetVersion && assetVersion !== version) {
          const next = new URL(window.location.href);
          next.searchParams.set("v", assetVersion);
          window.location.replace(next.toString());
        }
      })
      .catch(() => {});
  };

  checkVersion();

  // In development (localhost), poll for version changes to support hot reload of static assets.
  if (
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1" ||
    window.location.hostname.endsWith(".local")
  ) {
    setInterval(checkVersion, 2000);
  }
})();
