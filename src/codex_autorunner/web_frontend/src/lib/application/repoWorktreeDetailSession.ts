import type { ApiError, JsonRecord } from '$lib/api/client';
import type { RepoWorktreeDetailSnapshot } from '$lib/api/readModelContracts';
import type {
  ActionNotice,
  RetireStateTarget,
  RetireWorktreeTarget
} from '$lib/actions/repoWorktreeActions';
import {
  ensureRepoWorktreeIndexLoaded,
  ensureRepoDetailLoaded,
  ensureWorktreeDetailLoaded,
  invalidateReadModelTags,
  readModelEntityStore,
  readModelEntityTags,
  selectRepoSummaries,
  selectWorktreeSummaries,
  type ReadModelDependency,
  type ReadModelEntityStore,
  type ReadModelLoaderResult
} from '$lib/data';
import {
  mapContextspaceDocument,
  mapChatSummary,
  mapChatRunProgress,
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
  loadRepoWorktreeIndex?: typeof ensureRepoWorktreeIndexLoaded;
  loadRepoDetail?: typeof ensureRepoDetailLoaded;
  loadWorktreeDetail?: typeof ensureWorktreeDetailLoaded;
  syncRepoMain: (repoId: string) => Promise<{ ok: true } | { ok: false; error: ApiError }>;
  syncWorktree: (worktreeId: string) => Promise<{ ok: true } | { ok: false; error: ApiError }>;
  invalidateTags?: typeof invalidateReadModelTags;
  retireWorktree: (target: RetireWorktreeTarget) => Promise<ActionNotice | null>;
  retireState: (target: RetireStateTarget) => Promise<ActionNotice | null>;
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
    Pick<RepoWorktreeDetailSessionDependencies, 'loadRepoWorktreeIndex' | 'loadRepoDetail' | 'loadWorktreeDetail' | 'invalidateTags'>
  > &
    Omit<RepoWorktreeDetailSessionDependencies, 'loadRepoWorktreeIndex' | 'loadRepoDetail' | 'loadWorktreeDetail' | 'invalidateTags'>;
  private readonly store: Pick<ReadModelEntityStore, 'snapshot'>;
  private requestVersion = 0;
  private currentLoaderResult: ReadModelLoaderResult | null;
  state: RepoWorktreeDetailSessionState;

  constructor(options: RepoWorktreeDetailSessionOptions) {
    this.store = options.dependencies.store ?? readModelEntityStore;
    this.dependencies = {
      ...options.dependencies,
      loadRepoWorktreeIndex: options.dependencies.loadRepoWorktreeIndex ?? ensureRepoWorktreeIndexLoaded,
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
    const requestVersion = ++this.requestVersion;
    const cached = this.cachedSnapshot(owner);
    if (cached) {
      if (!this.isCurrentRequest(requestVersion, owner)) return;
      await this.applySnapshot(owner, cached);
      this.state = { ...this.state, loading: false };
      return;
    }
    let provisional = this.provisionalDetail(owner);
    if (!provisional) {
      await this.dependencies.loadRepoWorktreeIndex({ blocking: true });
      if (!this.isCurrentRequest(requestVersion, owner)) return;
      provisional = this.provisionalDetail(owner);
    }
    if (provisional) {
      if (!this.isCurrentRequest(requestVersion, owner)) return;
      this.state = { ...this.state, detail: provisional, loading: false };
      await this.load(false);
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

  async retireState(target: RetireStateTarget): Promise<void> {
    const result = await this.dependencies.retireState(target);
    if (!result) return;
    this.state = { ...this.state, notice: result };
    if (result.tone === 'success') await this.load();
  }

  async syncRepo(): Promise<void> {
    if (this.state.syncRepoBusy) return;
    const repoId = this.state.ownerKind === 'repo' ? this.state.ownerId : this.state.backingRepoId;
    if (this.state.ownerKind === 'worktree' && !repoId) {
      this.state = { ...this.state, notice: { tone: 'danger', message: 'Could not resolve parent repo for sync.' } };
      return;
    }

    this.state = { ...this.state, syncRepoBusy: true };
    try {
      const result =
        this.state.ownerKind === 'repo'
          ? await this.dependencies.syncRepoMain(this.state.ownerId)
          : await this.dependencies.syncWorktree(this.state.ownerId);
      if (!result.ok) {
        this.state = { ...this.state, notice: { tone: 'danger', message: result.error.message } };
        return;
      }
      await this.dependencies.invalidateTags(this.syncInvalidationTags(repoId ?? this.state.ownerId));
      this.state = {
        ...this.state,
        notice: {
          tone: 'success',
          message:
            this.state.ownerKind === 'repo'
              ? 'Synced default branch with origin.'
              : 'Synced worktree branch with upstream.'
        }
      };
      await this.load(false);
    } finally {
      this.state = { ...this.state, syncRepoBusy: false };
    }
  }

  private initialState(owner: OwnerRef, loaderResult: ReadModelLoaderResult | null): RepoWorktreeDetailSessionState {
    const cached = this.cachedSnapshot(owner);
    const provisional = cached ? null : this.provisionalDetail(owner);
    return {
      ownerKind: owner.kind,
      ownerId: owner.id,
      detail: provisional,
      loading: loaderResult?.status === 'cold' && !cached && !provisional,
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

  private provisionalDetail(owner: OwnerRef): RepoWorktreeDetailViewModel | null {
    const snapshot = this.store.snapshot();
    const repos = selectRepoSummaries(snapshot);
    const worktrees = selectWorktreeSummaries(snapshot);
    const found =
      owner.kind === 'repo'
        ? repos.some((repo) => repo.id === owner.id)
        : worktrees.some((worktree) => worktree.id === owner.id);
    if (!found) return null;
    return buildRepoWorktreeDetailViewModel(
      {
        repos,
        worktrees,
        runs: [],
        chats: [],
        tickets: [],
        artifacts: [],
        ticketsListLoaded: false
      },
      owner.kind,
      owner.id
    );
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
    runs: snapshot.runQueue.map(mapChatRunProgress),
    chats: snapshot.chatQueue.map(mapChatSummary),
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
