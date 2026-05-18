import type { ApiError, JsonRecord } from '$lib/api/client';
import type { RepoWorktreeDetailSnapshot } from '$lib/api/readModelContracts';
import type {
  ArchiveStateTarget,
  ActionNotice,
  RetireWorktreeTarget
} from '$lib/actions/repoWorktreeActions';
import {
  ensureRepoDetailLoaded,
  ensureWorktreeDetailLoaded,
  invalidateReadModelTags,
  readModelEntityStore,
  readModelEntityTags,
  type ReadModelDependency,
  type ReadModelEntityStore,
  type ReadModelLoaderResult
} from '$lib/data';
import {
  mapContextspaceDocument,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketSummary,
  mapWorktreeSummary
} from '$lib/viewModels/domain';
import {
  buildRepoWorktreeDetailViewModel,
  type RepoWorktreeDetailViewModel
} from '$lib/viewModels/repoWorktree';
import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';
import type { PartialPageIssue } from '$lib/api/client';

export type RepoWorktreeDetailOwnerKind = 'repo' | 'worktree';

export type RepoWorktreeDetailSessionState = {
  ownerKind: RepoWorktreeDetailOwnerKind;
  ownerId: string;
  detail: RepoWorktreeDetailViewModel | null;
  loading: boolean;
  error: ApiError | null;
  sectionIssues: PartialPageIssue[];
  notice: ActionNotice | null;
  syncRepoBusy: boolean;
  backingRepoId: string | null;
};

export type RepoWorktreeDetailSessionDependencies = {
  store?: Pick<ReadModelEntityStore, 'snapshot'>;
  loadRepoDetail?: typeof ensureRepoDetailLoaded;
  loadWorktreeDetail?: typeof ensureWorktreeDetailLoaded;
  syncRepoMain: (repoId: string) => Promise<{ ok: true } | { ok: false; error: ApiError }>;
  invalidateTags?: typeof invalidateReadModelTags;
  retireWorktree: (target: RetireWorktreeTarget) => Promise<ActionNotice | null>;
  archiveState: (target: ArchiveStateTarget) => Promise<ActionNotice | null>;
  currentPath?: () => string;
  redirect?: (path: string) => Promise<void> | void;
};

export type RepoWorktreeDetailSessionOptions = {
  ownerKind: RepoWorktreeDetailOwnerKind;
  ownerId: string;
  loaderResult?: ReadModelLoaderResult;
  dependencies: RepoWorktreeDetailSessionDependencies;
};

type OwnerRef = {
  kind: RepoWorktreeDetailOwnerKind;
  id: string;
};

export class RepoWorktreeDetailSession {
  private readonly dependencies: Required<
    Pick<RepoWorktreeDetailSessionDependencies, 'loadRepoDetail' | 'loadWorktreeDetail' | 'invalidateTags'>
  > &
    Omit<RepoWorktreeDetailSessionDependencies, 'loadRepoDetail' | 'loadWorktreeDetail' | 'invalidateTags'>;
  private readonly store: Pick<ReadModelEntityStore, 'snapshot'>;
  private requestVersion = 0;
  private currentLoaderResult: ReadModelLoaderResult | null;
  state: RepoWorktreeDetailSessionState;

  constructor(options: RepoWorktreeDetailSessionOptions) {
    this.store = options.dependencies.store ?? readModelEntityStore;
    this.dependencies = {
      ...options.dependencies,
      loadRepoDetail: options.dependencies.loadRepoDetail ?? ensureRepoDetailLoaded,
      loadWorktreeDetail: options.dependencies.loadWorktreeDetail ?? ensureWorktreeDetailLoaded,
      invalidateTags: options.dependencies.invalidateTags ?? invalidateReadModelTags
    };
    this.currentLoaderResult = options.loaderResult ?? null;
    this.state = this.initialState({ kind: options.ownerKind, id: options.ownerId }, options.loaderResult ?? null);
  }

  setOwner(ownerKind: RepoWorktreeDetailOwnerKind, ownerId: string, loaderResult?: ReadModelLoaderResult): void {
    if (ownerKind === this.state.ownerKind && ownerId === this.state.ownerId && loaderResult === this.currentLoaderResult) {
      return;
    }
    this.requestVersion += 1;
    this.currentLoaderResult = loaderResult ?? null;
    this.state = {
      ...this.initialState({ kind: ownerKind, id: ownerId }, loaderResult ?? null),
      notice: this.state.notice
    };
  }

  applyLoaderResult(loaderResult: ReadModelLoaderResult | null): void {
    this.currentLoaderResult = loaderResult;
    this.state = {
      ...this.state,
      loading: loaderResult?.status === 'cold' && !this.cachedSnapshot(this.owner()),
      error: loaderResult?.status === 'error' ? loaderResult.error : null
    };
  }

  async hydrate(): Promise<void> {
    const owner = this.owner();
    const cached = this.cachedSnapshot(owner);
    if (cached) {
      await this.applySnapshot(owner, cached);
      this.state = { ...this.state, loading: false };
      return;
    }
    await this.load(true);
  }

  async load(showLoading = true): Promise<void> {
    const owner = this.owner();
    const requestVersion = ++this.requestVersion;
    if (showLoading) this.state = { ...this.state, loading: true };
    this.state = { ...this.state, error: null, sectionIssues: [], backingRepoId: owner.kind === 'repo' ? owner.id : null };

    const result =
      owner.kind === 'repo'
        ? await this.dependencies.loadRepoDetail(owner.id, { refresh: true })
        : await this.dependencies.loadWorktreeDetail(owner.id, { refresh: true });
    if (!this.isCurrentRequest(requestVersion, owner)) return;

    if (result.status === 'error') {
      this.currentLoaderResult = result;
      this.state = { ...this.state, error: result.error, loading: false };
      return;
    }

    const snapshot = this.cachedSnapshot(owner);
    if (snapshot) {
      const redirected = await this.applySnapshot(owner, snapshot);
      if (redirected) return;
    }
    if (!this.isCurrentRequest(requestVersion, owner)) return;
    this.state = { ...this.state, loading: false };
  }

  async retireWorktree(target: RetireWorktreeTarget): Promise<void> {
    const result = await this.dependencies.retireWorktree(target);
    if (!result) return;
    this.state = { ...this.state, notice: result };
    if (result.tone === 'success') await this.load();
  }

  async archiveState(target: ArchiveStateTarget): Promise<void> {
    const result = await this.dependencies.archiveState(target);
    if (!result) return;
    this.state = { ...this.state, notice: result };
    if (result.tone === 'success') await this.load();
  }

  async syncRepo(): Promise<void> {
    if (this.state.syncRepoBusy) return;
    const repoId = this.state.ownerKind === 'repo' ? this.state.ownerId : this.state.backingRepoId;
    if (!repoId) {
      this.state = { ...this.state, notice: { tone: 'danger', message: 'Could not resolve parent repo for sync.' } };
      return;
    }

    this.state = { ...this.state, syncRepoBusy: true };
    try {
      const result = await this.dependencies.syncRepoMain(repoId);
      if (!result.ok) {
        this.state = { ...this.state, notice: { tone: 'danger', message: result.error.message } };
        return;
      }
      await this.dependencies.invalidateTags(this.syncInvalidationTags(repoId));
      this.state = { ...this.state, notice: { tone: 'success', message: 'Synced default branch with origin.' } };
      await this.load(false);
    } finally {
      this.state = { ...this.state, syncRepoBusy: false };
    }
  }

  private initialState(owner: OwnerRef, loaderResult: ReadModelLoaderResult | null): RepoWorktreeDetailSessionState {
    const cached = this.cachedSnapshot(owner);
    return {
      ownerKind: owner.kind,
      ownerId: owner.id,
      detail: null,
      loading: loaderResult?.status === 'cold' && !cached,
      error: loaderResult?.status === 'error' ? loaderResult.error : null,
      sectionIssues: [],
      notice: null,
      syncRepoBusy: false,
      backingRepoId: owner.kind === 'repo' ? owner.id : null
    };
  }

  private owner(): OwnerRef {
    return { kind: this.state.ownerKind, id: this.state.ownerId };
  }

  private cachedSnapshot(owner: OwnerRef): RepoWorktreeDetailSnapshot | undefined {
    const snapshot = this.store.snapshot();
    return owner.kind === 'repo' ? snapshot.repoDetails[owner.id] : snapshot.worktreeDetails[owner.id];
  }

  private async applySnapshot(owner: OwnerRef, snapshot: RepoWorktreeDetailSnapshot): Promise<boolean> {
    if (owner.kind === 'worktree') {
      const backingRepoId = parentRepoIdFromWorktreeSnapshot(snapshot, owner.id);
      this.state = { ...this.state, backingRepoId };
      const redirectTo = legacyWorktreeRedirectPath(this.dependencies.currentPath?.() ?? '', owner.id, backingRepoId);
      if (redirectTo) {
        await this.dependencies.redirect?.(redirectTo);
        return true;
      }
    }
    this.state = {
      ...this.state,
      detail: buildDetailFromSnapshot(snapshot, owner),
      backingRepoId: owner.kind === 'repo' ? owner.id : parentRepoIdFromWorktreeSnapshot(snapshot, owner.id)
    };
    return false;
  }

  private syncInvalidationTags(repoId: string): ReadModelDependency[] {
    const tags: ReadModelDependency[] = [readModelEntityTags.repoWorktreeIndex, readModelEntityTags.repo(repoId)];
    if (this.state.ownerKind === 'worktree') tags.push(readModelEntityTags.worktree(this.state.ownerId));
    return tags;
  }

  private isCurrentRequest(requestVersion: number, owner: OwnerRef): boolean {
    return (
      requestVersion === this.requestVersion &&
      owner.kind === this.state.ownerKind &&
      owner.id === this.state.ownerId
    );
  }
}

export function createRepoWorktreeDetailSession(
  options: RepoWorktreeDetailSessionOptions
): RepoWorktreeDetailSession {
  return new RepoWorktreeDetailSession(options);
}

export function buildDetailFromSnapshot(
  snapshot: RepoWorktreeDetailSnapshot,
  owner: OwnerRef
): RepoWorktreeDetailViewModel {
  const baseSource = {
    repos: owner.kind === 'repo' ? [mapRepoSummary(snapshot.identity)] : [],
    worktrees:
      owner.kind === 'repo'
        ? childrenFromTopology(snapshot.topology).map(mapWorktreeSummary)
        : [mapWorktreeSummary(snapshot.identity)],
    runs: snapshot.runQueue.map(mapPmaRunProgress),
    chats: snapshot.chatQueue.map(mapPmaChatSummary),
    tickets: snapshot.ticketQueue.map(mapTicketSummary),
    contextspaceDocs: snapshot.contextspaceSummary.map(mapContextspaceDocument),
    artifacts: snapshot.currentArtifacts.map(mapSurfaceArtifact)
  };
  return buildRepoWorktreeDetailViewModel(baseSource, owner.kind, owner.id);
}

function parentRepoIdFromWorktreeSnapshot(snapshot: RepoWorktreeDetailSnapshot, worktreeId: string): string | null {
  const matchedWorktree = [mapWorktreeSummary(snapshot.identity)].find((worktree) => worktree.id === worktreeId);
  return matchedWorktree?.repoId ?? null;
}

function childrenFromTopology(topology: Record<string, unknown>): JsonRecord[] {
  return Array.isArray(topology.children) ? (topology.children as JsonRecord[]) : [];
}
