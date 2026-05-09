import type { Reroute } from '@sveltejs/kit';
import { runtimeBasePath, stripRuntimeBasePath } from '$lib/runtime/basePath';

export const reroute: Reroute = ({ url }) => {
  const basePath = runtimeBasePath();
  if (!basePath) return;
  const pathname = stripRuntimeBasePath(url.pathname, basePath);
  if (pathname !== url.pathname) return pathname;
};
