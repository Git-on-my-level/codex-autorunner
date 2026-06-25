<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { breadcrumbsForPath } from '$lib/breadcrumbs';
  import { primaryNav, isActiveRoute } from '$lib/navigation';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { webApi } from '$lib/api/client';
  import { Palette, createPaletteStore, recentActionsSource, repoSource, scopeSource, threadSource, worktreeSource } from '$lib/palette';
  import {
    ensureChatIndexLoaded,
    ensureRepoWorktreeIndexLoaded,
    readModelEntityStore,
    selectChats,
    selectRepoSummaries,
    selectWorktreeSummaries
  } from '$lib/data';
  import { connectionStore, type ConnectionStatus } from '$lib/runtime/connectionStore.svelte';
  import { consumeHostedBearerFromLocation } from '$lib/runtime/hostedAuth';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import {
    applyThemePreference,
    attachThemeSchemeListener,
    detachThemeSchemeListener,
    THEME_STORAGE_KEY
  } from '$lib/theme';
  import { onDestroy, onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import '../app.css';
  import '../theme-presets.css';

  const REPO_ATTENTION_CHANGED_EVENT = 'car:repo-attention-changed';

  let { children }: { children: Snippet } = $props();
  let collapsed = $state(false);
  let mobileOpen = $state(false);
  let hubTitle = $state('Web Hub');
  let titleDraft = $state('Web Hub');
  let titleSaving = $state(false);
  let repoAttentionCount = $state(0);
  let repoAttentionRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  const currentPath = $derived(stripRuntimeBasePath(page.url.pathname));
  const breadcrumbs = $derived(breadcrumbsForPath(currentPath));

  const paletteStore = createPaletteStore(
    [recentActionsSource(), scopeSource()],
    {
      toggleSidebar: () => (collapsed = !collapsed),
      toggleMemory: () => void goto(href('/settings?memory=1')),
      goBack: () => window.history.back()
    },
    (path) => void goto(href(path))
  );

  onMount(() => {
    consumeHostedBearerFromLocation();
    attachThemeSchemeListener();
    try {
      if (localStorage.getItem(THEME_STORAGE_KEY) === null) {
        applyThemePreference('system');
      }
    } catch {
      /* private mode / quota */
    }
    void loadHubState();
    void loadRepoAttention();
    const unsubscribeReadModels = readModelEntityStore.subscribe(refreshPaletteSources);
    window.addEventListener(REPO_ATTENTION_CHANGED_EVENT, handleRepoAttentionChanged);
    void loadPaletteSources();
    return () => {
      unsubscribeReadModels();
      window.removeEventListener(REPO_ATTENTION_CHANGED_EVENT, handleRepoAttentionChanged);
      clearRepoAttentionRefreshTimer();
    };
  });

  onDestroy(() => {
    paletteStore.destroy();
    detachThemeSchemeListener();
  });

  const closeMobile = () => {
    mobileOpen = false;
  };

  const hubGlyph = $derived((hubTitle.trim().charAt(0) || 'W').toUpperCase());

  async function loadHubState(): Promise<void> {
    const result = await webApi.hub.getState();
    if (!result.ok) return;
    hubTitle = result.data.title;
    titleDraft = result.data.title;
  }

  async function loadRepoAttention(): Promise<void> {
    const result = await webApi.hub.getDashboard();
    if (!result.ok) return;
    const raw = result.data.raw as Record<string, unknown>;
    const items = Array.isArray(raw.items) ? raw.items : [];
    repoAttentionCount = items.filter((item) => {
      if (!item || typeof item !== 'object') return false;
      const record = item as Record<string, unknown>;
      return record.item_type === 'run_dispatch' || record.next_action === 'reply_and_resume';
    }).length;
  }

  function handleRepoAttentionChanged(event: Event): void {
    const delta = event instanceof CustomEvent ? numericDelta(event.detail) : null;
    if (delta !== null) repoAttentionCount = Math.max(0, repoAttentionCount + delta);
    clearRepoAttentionRefreshTimer();
    repoAttentionRefreshTimer = setTimeout(() => {
      repoAttentionRefreshTimer = null;
      void loadRepoAttention();
    }, 8000);
  }

  function numericDelta(detail: unknown): number | null {
    if (!detail || typeof detail !== 'object') return null;
    const value = (detail as Record<string, unknown>).delta;
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function clearRepoAttentionRefreshTimer(): void {
    if (repoAttentionRefreshTimer === null) return;
    clearTimeout(repoAttentionRefreshTimer);
    repoAttentionRefreshTimer = null;
  }

  async function loadPaletteSources(): Promise<void> {
    await Promise.all([
      ensureRepoWorktreeIndexLoaded({ limit: 2000, blocking: true }),
      ensureChatIndexLoaded({ limit: 1000 }, { blocking: true })
    ]);
    refreshPaletteSources();
  }

  function refreshPaletteSources(): void {
    const state = readModelEntityStore.snapshot();
    paletteStore.updateSources([
      recentActionsSource(),
      threadSource(selectChats(state)),
      repoSource(selectRepoSummaries(state)),
      worktreeSource(selectWorktreeSummaries(state)),
      scopeSource()
    ]);
  }

  async function saveHubTitle(): Promise<void> {
    const next = titleDraft.trim() || 'Web Hub';
    if (next === hubTitle || titleSaving) {
      titleDraft = hubTitle;
      return;
    }
    titleSaving = true;
    const result = await webApi.hub.updateState({ title: next });
    titleSaving = false;
    if (!result.ok) {
      titleDraft = hubTitle;
      return;
    }
    hubTitle = result.data.title;
    titleDraft = result.data.title;
  }

  function handleTitleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      (event.currentTarget as HTMLInputElement | null)?.blur();
    } else if (event.key === 'Escape') {
      titleDraft = hubTitle;
      (event.currentTarget as HTMLInputElement | null)?.blur();
    }
  }

  const connectionStatus = $derived<ConnectionStatus>(connectionStore.status);
  const connectionLabel = $derived(
    connectionStatus === 'connected'
      ? 'Live'
      : connectionStatus === 'connecting'
        ? 'Connecting'
        : connectionStatus === 'interrupted'
          ? 'Reconnecting'
          : connectionStatus === 'offline'
            ? 'Offline'
            : 'Idle'
  );
  const connectionTitle = $derived(
    connectionStatus === 'connected'
      ? 'Live updates connected'
      : connectionStatus === 'connecting'
        ? 'Connecting to the hub stream'
        : connectionStatus === 'interrupted'
          ? 'Reconnecting to the hub stream'
          : connectionStatus === 'offline'
            ? 'Browser is offline'
            : 'No active stream'
  );

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      if (paletteStore.open) {
        paletteStore.closePalette();
        return;
      }
      closeMobile();
    }
    if (paletteStore.handleKeydown(event)) return;
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<svelte:head>
  <title>{hubTitle}</title>
</svelte:head>

<div class:sidebar-collapsed={collapsed} class:mobile-open={mobileOpen} class="app-shell">
  <aside class="sidebar" aria-label="Primary navigation">
    <div class="brand-row">
      <div class="brand-mark" aria-label={`${hubTitle} home`}>
        <a class="brand-glyph" href={href('/chats')} onclick={closeMobile} aria-label={`${hubTitle} home`}>{hubGlyph}</a>
        <span class="brand-copy">
          <input
            class="brand-title-input"
            bind:value={titleDraft}
            aria-label="Hub title"
            disabled={titleSaving}
            onblur={saveHubTitle}
            onkeydown={handleTitleKeydown}
          />
          <span class="brand-subtitle">Chats</span>
        </span>
      </div>
      <button
        class="icon-button desktop-collapse"
        type="button"
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        onclick={() => (collapsed = !collapsed)}
      >
        <span aria-hidden="true">{collapsed ? '›' : '‹'}</span>
      </button>
    </div>

    <nav class="nav-list">
      <div class="nav-group" aria-label="Navigation">
        {#each primaryNav as item}
          <a
            class:active={isActiveRoute(stripRuntimeBasePath(page.url.pathname), item.href)}
            class="nav-link"
            href={href(item.href)}
            onclick={closeMobile}
          >
            <span class="nav-initial" aria-hidden="true">{item.label.slice(0, 1)}</span>
            <span class="nav-label">{item.label}</span>
            {#if item.href === '/repos' && repoAttentionCount > 0}
              <span class="nav-attention-badge" aria-label={`${repoAttentionCount} repo item${repoAttentionCount === 1 ? '' : 's'} need response`}>
                {repoAttentionCount > 9 ? '9+' : repoAttentionCount}
              </span>
            {/if}
          </a>
        {/each}
      </div>
    </nav>

  </aside>

  <button class="mobile-scrim" type="button" aria-label="Close navigation" onclick={closeMobile}></button>

  <div class="workspace">
    <header class="topbar">
      <button
        class="icon-button mobile-menu"
        type="button"
        aria-label="Open navigation"
        aria-expanded={mobileOpen}
        title="Open navigation"
        onclick={() => (mobileOpen = true)}
      >
        <span aria-hidden="true">≡</span>
      </button>
      <nav class="topbar-copy" aria-label="Breadcrumb">
        {#each breadcrumbs as crumb, index}
          {#if crumb.href && index < breadcrumbs.length - 1}
            <a class="topbar-crumb" href={href(crumb.href)}>{crumb.label}</a>
          {:else}
            <span class="topbar-crumb current">{crumb.label}</span>
          {/if}
        {/each}
      </nav>
      <div
        class={`hub-status status-${connectionStatus}`}
        role="status"
        title={connectionTitle}
        aria-label={connectionTitle}
      >
        <span class="status-dot" aria-hidden="true"></span>
        <span class="hub-status-label">{connectionLabel}</span>
      </div>
    </header>

    <main class="content-shell">
      {@render children()}
    </main>
  </div>
</div>

<Palette store={paletteStore} />
<ConfirmDialog />
