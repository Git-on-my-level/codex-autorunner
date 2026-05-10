declare global {
  var __CAR_BASE_PATH__: string | undefined;
}

const UNPREFIXED_SCHEMES = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;

export function normalizeBasePath(basePath: string | null | undefined): string {
  if (!basePath) return '';
  const trimmed = basePath.trim();
  if (!trimmed || trimmed === '/') return '';
  const withLeadingSlash = trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
  return withLeadingSlash.replace(/\/+$/, '');
}

export function runtimeBasePath(): string {
  return normalizeBasePath(globalThis.__CAR_BASE_PATH__);
}

export function withRuntimeBasePath(path: string, basePath = runtimeBasePath()): string {
  const normalizedBasePath = normalizeBasePath(basePath);
  if (!normalizedBasePath) return path;
  if (!path || path.startsWith('#') || UNPREFIXED_SCHEMES.test(path)) return path;
  if (!path.startsWith('/')) return path;
  if (path === normalizedBasePath || path.startsWith(`${normalizedBasePath}/`)) return path;
  return `${normalizedBasePath}${path}`;
}

export function stripRuntimeBasePath(pathname: string, basePath = runtimeBasePath()): string {
  const normalizedBasePath = normalizeBasePath(basePath);
  if (!normalizedBasePath) return pathname;
  if (pathname === normalizedBasePath) return '/';
  if (pathname.startsWith(`${normalizedBasePath}/`)) return pathname.slice(normalizedBasePath.length) || '/';
  return pathname;
}
