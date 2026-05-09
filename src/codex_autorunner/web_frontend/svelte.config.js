import adapter from '@sveltejs/adapter-static';

function webVersion() {
  return process.env.CODEX_WEB_STATIC_VERSION || 'source';
}

const config = {
  kit: {
    adapter: adapter({
      fallback: 'index.html',
      pages: '../web_static',
      assets: '../web_static'
    }),
    version: {
      name: webVersion()
    },
    prerender: {
      handleUnseenRoutes: 'ignore'
    },
    paths: {}
  }
};

export default config;
