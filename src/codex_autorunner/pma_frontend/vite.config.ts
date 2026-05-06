import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [sveltekit()],
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
});
