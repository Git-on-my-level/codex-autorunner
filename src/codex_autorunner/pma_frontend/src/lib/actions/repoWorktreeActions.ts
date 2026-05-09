import { pmaApi, type JsonRecord } from '$lib/api/client';

export type ActionNotice = {
  message: string;
  tone: 'success' | 'warning' | 'danger';
};

export type CleanupWorktreeTarget = {
  id: string;
  label: string;
  chatBound: boolean;
  cleanupBlockedByChatBinding: boolean;
};

export type ArchiveStateTarget = {
  kind: 'repo' | 'worktree';
  id: string;
  label: string;
  hasCarState: boolean;
  unboundManagedThreadCount: number;
};

export async function confirmAndCleanupWorktree(target: CleanupWorktreeTarget): Promise<ActionNotice | null> {
  if (typeof window === 'undefined') return null;
  const ok = window.confirm(
    `Cleanup worktree "${target.label}"?\n\nCAR will archive a review snapshot, stop any runner, remove the worktree checkout, and unregister it from the hub.`
  );
  if (!ok) return null;

  const requiresTypedConfirmation = target.chatBound || target.cleanupBlockedByChatBinding;
  const confirmationText = `cleanup ${target.id}`;
  let forceAttestation: string | null = null;
  if (requiresTypedConfirmation) {
    const entered = window.prompt(
      `This worktree is bound to chat state. Type "${confirmationText}" to confirm cleanup.`
    );
    if (entered !== confirmationText) {
      return { tone: 'warning', message: 'Cleanup cancelled; confirmation text did not match.' };
    }
    forceAttestation = entered;
  }

  const result = await pmaApi.hub.cleanupWorktree({
    worktreeRepoId: target.id,
    archive: true,
    force: requiresTypedConfirmation,
    forceAttestation,
    forceArchive: false,
    archiveNote: null
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  return { tone: 'success', message: `Cleaned up worktree ${target.label}.` };
}

export async function confirmAndArchiveState(target: ArchiveStateTarget): Promise<ActionNotice | null> {
  if (typeof window === 'undefined') return null;
  const subject = target.kind === 'repo' ? 'repo' : 'worktree';
  const stateText = target.hasCarState
    ? 'archive reviewable CAR runtime artifacts and reset local CAR state'
    : 'archive stale non-chat-bound managed threads; CAR state is already clean';
  const threadText =
    target.unboundManagedThreadCount > 0
      ? `\n\nThis will also archive ${target.unboundManagedThreadCount} stale non-chat-bound managed thread${
          target.unboundManagedThreadCount === 1 ? '' : 's'
        }.`
      : '';
  const ok = window.confirm(
    `Archive ${subject} "${target.label}"?\n\nCAR will ${stateText}. Git files and active chat bindings are not deleted.${threadText}`
  );
  if (!ok) return null;

  const result = await pmaApi.hub.archiveState({
    kind: target.kind,
    id: target.id,
    archiveNote: null
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };

  const payload = result.data;
  const snapshotText = stringValue(payload.snapshot_id) ?? 'managed threads only';
  const threadCount = numberValue(payload.archived_thread_count);
  const threadTextSuffix =
    threadCount > 0 ? ` and ${threadCount} managed thread${threadCount === 1 ? '' : 's'}` : '';
  return {
    tone: 'success',
    message: `Archived ${subject} ${target.label} (${snapshotText}${threadTextSuffix}).`
  };
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberValue(value: JsonRecord[keyof JsonRecord]): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}
