import type { ReadModelDepends, ReadModelLoaderOptions, ReadModelLoaderResult } from '$lib/data';

export type LoadReadModelRouteOptions = {
  depends?: ReadModelDepends;
  params?: Record<string, string | undefined>;
  loaderOptions?: ReadModelLoaderOptions;
};

export function readModelLoaderOptions(options: LoadReadModelRouteOptions): ReadModelLoaderOptions {
  return {
    ...options.loaderOptions,
    depends: options.depends,
    refresh: options.loaderOptions?.refresh ?? true,
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
