(() => {
  interface WindowExtensions {
    __CAR_AUTH_TOKEN?: string;
    __CAR_BASE_PREFIX?: string;
    __CAR_REPO_ID?: string;
    __CAR_BASE_PATH?: string;
    __AUTH_TOKEN_PRESENT?: boolean;
    __assetSuffix?: string;
  }

  const windowExt = window as unknown as WindowExtensions;
  const AUTH_TOKEN_KEY = "car_auth_token";
  const url = new URL(window.location.href);
  const params = url.searchParams;
  let token: string | null = params.get("token");
  if (token) {
    windowExt.__CAR_AUTH_TOKEN = token;
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
      if (token) windowExt.__CAR_AUTH_TOKEN = token;
    } catch (_err) {
      // Ignore storage errors; token may still be in memory.
    }
  }

  const normalizeBase = (base: string): string => {
    if (!base || base === "/") return "";
    let normalized = base.startsWith("/") ? base : `/${base}`;
    while (normalized.endsWith("/") && normalized.length > 1) {
      normalized = normalized.slice(0, -1);
    }
    return normalized === "/" ? "" : normalized;
  };

  const detectBasePrefix = (path: string): string => {
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
  const repoId: string | null = repoMatch && repoMatch[1] ? repoMatch[1] : null;

  windowExt.__CAR_BASE_PREFIX = basePrefix || "";
  windowExt.__CAR_REPO_ID = repoId;
  windowExt.__CAR_BASE_PATH = repoId ? `${basePrefix}/repos/${repoId}` : basePrefix;
  windowExt.__AUTH_TOKEN_PRESENT = Boolean(windowExt.__CAR_AUTH_TOKEN);

  const base = basePrefix || "/";
  const baseTag = document.createElement("base");
  (baseTag as HTMLBaseElement).href = base.endsWith("/") ? base : `${base}/`;
  document.head.appendChild(baseTag);

  const readAssetVersion = (): string => {
    const queryVersion = new URLSearchParams(window.location.search).get("v");
    if (queryVersion) return queryVersion;
    const bootstrapScript =
      document.currentScript ||
      document.querySelector('script[data-car-bootstrap]');
    if (bootstrapScript && (bootstrapScript as HTMLScriptElement).src) {
      try {
        const scriptUrl = new URL((bootstrapScript as HTMLScriptElement).src, window.location.href);
        return scriptUrl.searchParams.get("v") || "";
      } catch (_err) {
        return "";
      }
    }
    return "";
  };

  const version = readAssetVersion();
  const suffix = version ? `?v=${encodeURIComponent(version)}` : "";
  windowExt.__assetSuffix = suffix;

  const addStylesheet = (href: string): void => {
    const link = document.createElement("link");
    (link as HTMLLinkElement).rel = "stylesheet";
    (link as HTMLLinkElement).href = `${href}${suffix}`;
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

  const checkVersion = (): void => {
    fetch(normalizedVersionPath, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const assetVersion = data && (data as { asset_version?: string }).asset_version;
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
