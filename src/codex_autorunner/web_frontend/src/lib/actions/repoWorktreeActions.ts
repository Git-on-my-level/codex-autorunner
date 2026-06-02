import { webApi, type JsonRecord } from '$lib/api/client';
import {
  confirmDialog,
  confirmDialogTyped
} from '$lib/components/confirmDialog';
import {
  invalidateReadModelTags,
  readModelEntityTags
} from '$lib/data';

export type ActionNotice = {
  message: string;
  tone: 'success' | 'warning' | 'danger';
  navigateTo?: string;
};

export type RetireWorktreeTarget = {
  id: string;
  label: string;
  chatBound: boolean;
  cleanupBlockedByChatBinding: boolean;
};

export type ArchiveWorktreeTarget = {
  id: string;
  label: string;
  archived: boolean;
};

export type RetireStateTarget = {
  kind: 'repo' | 'worktree';
  id: string;
  label: string;
  hasCarState: boolean;
  unboundManagedThreadCount: number;
};

export async function confirmAndRetireWorktree(target: RetireWorktreeTarget): Promise<ActionNotice | null> {
  if (typeof window === 'undefined') return null;

  const requiresTypedConfirmation = target.chatBound || target.cleanupBlockedByChatBinding;
  const confirmationText = `retire ${target.id}`;
  let forceAttestation: string | null = null;

  if (requiresTypedConfirmation) {
    const entered = await confirmDialogTyped({
      title: `Retire worktree "${target.label}"`,
      message:
        `This worktree is bound to chat state. CAR will preserve review artifacts, stop any runner, remove the worktree checkout, and unregister it from the hub.`,
      confirmText: 'Retire',
      danger: true,
      requireType: confirmationText
    });
    if (entered === null) return null;
    if (entered !== confirmationText) {
      return { tone: 'warning', message: 'Retire cancelled; confirmation text did not match.' };
    }
    forceAttestation = entered;
  } else {
    const ok = await confirmDialog({
      title: `Retire worktree "${target.label}"`,
      message:
        'CAR will preserve review artifacts, stop any runner, remove the worktree checkout, and unregister it from the hub.',
      confirmText: 'Retire',
      danger: true
    });
    if (!ok) return null;
  }

  const result = await webApi.hub.retireWorktree({
    worktreeRepoId: target.id,
    force: requiresTypedConfirmation,
    forceAttestation,
    forceRetire: false,
    retireNote: null
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([
    readModelEntityTags.repoWorktreeIndex,
    readModelEntityTags.worktree(target.id)
  ]);
  return { tone: 'success', message: `Retired worktree ${target.label}.` };
}

export async function archiveWorktree(target: ArchiveWorktreeTarget): Promise<ActionNotice> {
  const result = await webApi.hub.archiveWorktree({
    worktreeRepoId: target.id,
    archived: !target.archived
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([
    readModelEntityTags.repoWorktreeIndex,
    readModelEntityTags.worktree(target.id)
  ]);
  return {
    tone: 'success',
    message: target.archived ? `Unarchived worktree ${target.label}.` : `Archived worktree ${target.label}.`
  };
}

export async function confirmAndRetireState(target: RetireStateTarget): Promise<ActionNotice | null> {
  if (typeof window === 'undefined') return null;
  const subject = target.kind === 'repo' ? 'repo' : 'worktree';
  const stateText = target.hasCarState
    ? 'retire reviewable CAR runtime artifacts and reset local CAR state'
    : 'retire stale non-chat-bound managed threads; CAR state is already clean';
  const threadText =
    target.unboundManagedThreadCount > 0
      ? `\n\nThis will also retire ${target.unboundManagedThreadCount} stale non-chat-bound managed thread${
          target.unboundManagedThreadCount === 1 ? '' : 's'
        }.`
      : '';
  const ok = await confirmDialog({
    title: `Retire ${subject} "${target.label}"`,
    message: `CAR will ${stateText}. Git files and active chat bindings are not deleted.${threadText}`,
    confirmText: 'Retire',
    danger: true
  });
  if (!ok) return null;

  const result = await webApi.hub.retireState({
    kind: target.kind,
    id: target.id,
    retireNote: null
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([
    readModelEntityTags.repoWorktreeIndex,
    target.kind === 'repo' ? readModelEntityTags.repo(target.id) : readModelEntityTags.worktree(target.id)
  ]);

  const payload = result.data;
  const snapshotText = stringValue(payload.snapshot_id) ?? 'managed threads only';
  const threadCount = numberValue(payload.retired_thread_count);
  const threadTextSuffix =
    threadCount > 0 ? ` and ${threadCount} managed thread${threadCount === 1 ? '' : 's'}` : '';
  return {
    tone: 'success',
    message: `Retired ${subject} ${target.label} (${snapshotText}${threadTextSuffix}).`
  };
}

export type CreateRepoInput = {
  repoId?: string;
  gitUrl?: string;
};

export async function submitCreateRepo(input: CreateRepoInput): Promise<ActionNotice> {
  const repoId = input.repoId?.trim() || undefined;
  const gitUrl = input.gitUrl?.trim() || undefined;
  if (!repoId && !gitUrl) {
    return { tone: 'warning', message: 'Provide a repo name or a git URL.' };
  }
  const result = await webApi.hub.createRepo({ repoId, gitUrl });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([readModelEntityTags.repoWorktreeIndex]);
  const label = stringValue(result.data.repo_id) ?? repoId ?? gitUrl ?? 'repo';
  const createdRepoId = stringValue(result.data.id) ?? stringValue(result.data.repo_id) ?? repoId ?? null;
  return {
    tone: 'success',
    message: gitUrl ? `Cloned ${label}.` : `Created repo ${label}.`,
    ...(createdRepoId ? { navigateTo: `/repos/${encodeURIComponent(createdRepoId)}` } : {})
  };
}

export type CreateWorktreeInput = {
  baseRepoId: string;
  baseRepoLabel: string;
  branch: string;
};

export async function submitCreateWorktree(input: CreateWorktreeInput): Promise<ActionNotice> {
  const branch = input.branch.trim();
  if (!branch) {
    return { tone: 'warning', message: 'Branch name is required.' };
  }
  const result = await webApi.hub.createWorktree({
    baseRepoId: input.baseRepoId,
    branch
    // startPoint omitted on purpose — backend defaults to origin/<default-branch>
    // after a fresh `git fetch --prune origin`.
  });
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([
    readModelEntityTags.repoWorktreeIndex,
    readModelEntityTags.repo(input.baseRepoId)
  ]);
  const worktreeId =
    stringValue(result.data.id) ??
    stringValue(result.data.repo_id) ??
    stringValue(result.data.worktree_repo_id) ??
    stringValue(result.data.worktree_id);
  const parentRepoId = stringValue(result.data.worktree_of) ?? stringValue(result.data.parent_repo_id) ?? input.baseRepoId;
  return {
    tone: 'success',
    message: `Created worktree ${branch} from origin/main on ${input.baseRepoLabel}.`,
    ...(worktreeId
      ? { navigateTo: `/repos/${encodeURIComponent(parentRepoId)}/worktrees/${encodeURIComponent(worktreeId)}` }
      : {})
  };
}

export type SetWorktreeSetupInput = {
  repoId: string;
  repoLabel: string;
  commands: string[];
};

export async function submitSetWorktreeSetup(input: SetWorktreeSetupInput): Promise<ActionNotice> {
  const cleaned = input.commands.map((cmd) => cmd.trim()).filter((cmd) => cmd.length > 0);
  const result = await webApi.hub.setWorktreeSetup(input.repoId, cleaned);
  if (!result.ok) return { tone: 'danger', message: result.error.message };
  await invalidateReadModelTags([
    readModelEntityTags.repoWorktreeIndex,
    readModelEntityTags.repo(input.repoId)
  ]);
  return {
    tone: 'success',
    message: cleaned.length
      ? `Saved ${cleaned.length} setup command${cleaned.length === 1 ? '' : 's'} for ${input.repoLabel}.`
      : `Cleared setup commands for ${input.repoLabel}.`
  };
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberValue(value: JsonRecord[keyof JsonRecord]): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}
