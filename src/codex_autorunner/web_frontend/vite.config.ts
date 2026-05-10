import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig(() => {
  const hubTarget =
    process.env.CAR_HUB_PROXY_TARGET?.trim() ||
    `http://127.0.0.1:${process.env.CAR_HUB_PROXY_PORT ?? '4173'}`;

  return {
    plugins: [sveltekit()],
    server: {
      proxy: {
        '/hub': { target: hubTarget, changeOrigin: true, ws: true },
        '/api': { target: hubTarget, changeOrigin: true },
        '/health': { target: hubTarget, changeOrigin: true },
        '/repos': {
          target: hubTarget,
          changeOrigin: true,
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
