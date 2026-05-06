import adapter from '@sveltejs/adapter-static';

function pmaVersion() {
  return process.env.CODEX_PMA_STATIC_VERSION || 'source';
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
