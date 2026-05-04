import adapter from '@sveltejs/adapter-static';

const config = {
  kit: {
    adapter: adapter({
      fallback: 'index.html',
      pages: '../pma_static',
      assets: '../pma_static'
    }),
    prerender: {
      handleUnseenRoutes: 'ignore'
    },
    paths: {}
  }
};

export default config;
