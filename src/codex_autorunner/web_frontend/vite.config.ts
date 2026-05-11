import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig(() => {
  const hubTarget =
    process.env.CAR_HUB_PROXY_TARGET?.trim() ||
    `http://127.0.0.1:${process.env.CAR_HUB_PROXY_PORT ?? '4173'}`;

  return {
    plugins: [sveltekit()],
    server: {
      // Keep the browser Host (Vite dev URL) on proxied hub requests — do not rewrite
      // Host to the hub target. HostOriginMiddleware compares Origin to scheme+Host;
      // changeOrigin:true breaks that for split-port dev (5173 UI vs 4173 API) while
      // production serves the built SPA from the hub (single origin, no proxy).
      proxy: {
        '/hub': { target: hubTarget, changeOrigin: false, ws: true },
        '/api': { target: hubTarget, changeOrigin: false },
        '/health': { target: hubTarget, changeOrigin: false },
        '/repos': {
          target: hubTarget,
          changeOrigin: false,
          bypass(req) {
            const path = req.url?.split('?')[0] ?? '';
            if (/^\/repos\/[^/]+\/api(\/|$)/.test(path)) {
              return false;
            }
            return req.url;
          }
        }
      }
    },
    build: {
      // Reduce cross-run variance in Rollup chunk splitting/minification so consecutive
      // `pnpm run build` outputs match what scripts/check.sh expects (WT vs index).
      rollupOptions: {
        maxParallelFileOps: 1
      }
    },
    test: {
      include: ['src/**/*.test.ts']
    }
  };
});
