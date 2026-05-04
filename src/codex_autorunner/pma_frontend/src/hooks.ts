import type { Reroute } from '@sveltejs/kit';

declare global {
  var __CAR_BASE_PATH__: string | undefined;
}

export const reroute: Reroute = ({ url }) => {
  const basePath = globalThis.__CAR_BASE_PATH__?.replace(/\/+$/, '');
  if (!basePath) return;
  if (url.pathname === basePath) return '/';
  if (url.pathname.startsWith(`${basePath}/`)) return url.pathname.slice(basePath.length) || '/';
};
