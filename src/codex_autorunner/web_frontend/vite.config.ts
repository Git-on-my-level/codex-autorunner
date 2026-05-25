import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
import type { ProxyOptions } from 'vite';
import type { ServerResponse } from 'node:http';

function isServerResponse(response: unknown): response is ServerResponse {
  return (
    typeof response === 'object' &&
    response !== null &&
    'writeHead' in response &&
    'end' in response
  );
}

function hubProxy(target: string, options: ProxyOptions = {}): ProxyOptions {
  return {
    target,
    changeOrigin: false,
    ...options,
    configure(proxy, proxyOptions) {
      options.configure?.(proxy, proxyOptions);
      proxy.on('error', (error, request, response) => {
        if (!isServerResponse(response) || response.headersSent) return;
        const code = typeof error === 'object' && error && 'code' in error
          ? String((error as { code?: unknown }).code)
          : 'proxy_error';
        const detail = [
          `Hub proxy failed while forwarding ${request.method ?? 'GET'} ${request.url ?? ''}.`,
          `Target ${target} is not reachable.`,
          code ? `Underlying error: ${code}` : null
        ].filter(Boolean).join(' ');
        response.writeHead(502, { 'content-type': 'application/json' });
        response.end(JSON.stringify({
          code: 'hub_proxy_error',
          detail,
          proxy_target: target,
          upstream_error_code: code,
          request_path: request.url ?? null
        }));
      });
    }
  };
}

export default defineConfig(() => {
  const hubTarget =
    process.env.CAR_HUB_PROXY_TARGET?.trim() ||
    `http://127.0.0.1:${process.env.CAR_HUB_PROXY_PORT ?? '4173'}`;
  const hubBasePath = (process.env.CAR_HUB_PROXY_BASE_PATH?.trim() || '/car').replace(/\/+$/, '');

  return {
    plugins: [sveltekit()],
    server: {
      // Keep the browser Host (Vite dev URL) on proxied hub requests — do not rewrite
      // Host to the hub target. HostOriginMiddleware compares Origin to scheme+Host;
      // changeOrigin:true breaks that for split-port dev (5173 UI vs 4173 API) while
      // production serves the built SPA from the hub (single origin, no proxy).
      proxy: {
        '/hub': hubProxy(hubTarget, { ws: true }),
        '/api': hubProxy(hubTarget),
        '/health': hubProxy(hubTarget),
        [`${hubBasePath}/hub`]: hubProxy(hubTarget, { ws: true }),
        [`${hubBasePath}/api`]: hubProxy(hubTarget),
        [`${hubBasePath}/health`]: hubProxy(hubTarget),
        '/repos': hubProxy(hubTarget, {
          bypass(req) {
            const path = req.url?.split('?')[0] ?? '';
            if (/^\/repos\/[^/]+\/api(\/|$)/.test(path)) {
              return false;
            }
            return req.url;
          }
        })
      }
    },
    build: {},
    test: {
      include: ['src/**/*.test.ts']
    }
  };
});
