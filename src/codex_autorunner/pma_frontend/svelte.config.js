import adapter from '@sveltejs/adapter-static';
import * as child_process from 'node:child_process';

function pmaVersion() {
  try {
    return child_process.execSync('git rev-parse HEAD', { encoding: 'utf8' }).trim();
  } catch {
    return 'source';
  }
}

const config = {
  kit: {
    adapter: adapter({
      fallback: 'index.html',
      pages: '../pma_static',
      assets: '../pma_static'
    }),
    version: {
      name: pmaVersion()
    },
    prerender: {
      handleUnseenRoutes: 'ignore'
    },
    paths: {}
  }
};

export default config;
