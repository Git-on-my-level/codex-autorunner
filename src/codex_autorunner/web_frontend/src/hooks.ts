import type { Reroute } from '@sveltejs/kit';
import { runtimeBasePath, stripRuntimeBasePath } from '$lib/runtime/basePath';

export const reroute: Reroute = ({ url }) => {
  const basePath = runtimeBasePath();
  const pathname = basePath ? stripRuntimeBasePath(url.pathname, basePath) : url.pathname;
  if (pathname.startsWith('/chats/')) return '/chats';
  if (basePath && pathname !== url.pathname) return pathname;
};
