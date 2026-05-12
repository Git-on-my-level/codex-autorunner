import { invalidate } from '$app/navigation';
import type { ApiResult } from '$lib/api/client';
import type { ReadModelDependency } from './readModelLoaders';

export type ReadModelInvalidator = (dependency: ReadModelDependency) => Promise<void>;

export async function invalidateReadModelTags(
  tags: Iterable<ReadModelDependency>,
  invalidator: ReadModelInvalidator = invalidate
): Promise<void> {
  const uniqueTags = [...new Set(tags)];
  await Promise.all(uniqueTags.map((tag) => invalidator(tag)));
}

export async function invalidateReadModelTagsOnSuccess<T>(
  result: ApiResult<T>,
  tags: Iterable<ReadModelDependency>,
  invalidator?: ReadModelInvalidator
): Promise<ApiResult<T>> {
  if (result.ok) await invalidateReadModelTags(tags, invalidator);
  return result;
}

export async function mutateAndInvalidate<T>(
  tags: Iterable<ReadModelDependency>,
  mutation: () => Promise<ApiResult<T>>,
  invalidator?: ReadModelInvalidator
): Promise<ApiResult<T>> {
  const result = await mutation();
  return invalidateReadModelTagsOnSuccess(result, tags, invalidator);
}
