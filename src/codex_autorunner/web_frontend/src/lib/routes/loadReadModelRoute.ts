import type { ReadModelDepends, ReadModelLoaderOptions, ReadModelLoaderResult } from '$lib/data';

export type LoadReadModelRouteOptions = {
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
};

export function readModelLoaderOptions(options: LoadReadModelRouteOptions): ReadModelLoaderOptions {
  return {
    ...options.loaderOptions,
    depends: options.depends,
    blocking: options.loaderOptions?.blocking ?? false
  };
}

export async function loadReadModelRoute(
  options: LoadReadModelRouteOptions & {
    load: (loaderOptions: ReadModelLoaderOptions) => Promise<ReadModelLoaderResult>;
  }
): Promise<ReadModelLoaderResult> {
  return options.load(readModelLoaderOptions(options));
}
