<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { breadcrumbsForPath } from '$lib/breadcrumbs';
  import { primaryNav, isActiveRoute } from '$lib/navigation';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { Palette, createPaletteStore, scopeSource } from '$lib/palette';
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

  let { children }: { children: Snippet } = $props();
  let collapsed = $state(false);
  let mobileOpen = $state(false);
  const currentPath = $derived(stripRuntimeBasePath(page.url.pathname));
  const breadcrumbs = $derived(breadcrumbsForPath(currentPath));

  const paletteStore = createPaletteStore(
    [scopeSource([], [])],
    {
      toggleSidebar: () => (collapsed = !collapsed),
      toggleMemory: () => void goto(href('/settings?memory=1')),
      goBack: () => window.history.back()
    },
    (path) => void goto(href(path))
  );

  onMount(() => {
    attachThemeSchemeListener();
    try {
      if (localStorage.getItem(THEME_STORAGE_KEY) === null) {
        applyThemePreference('system');
      }
    } catch {
      /* private mode / quota */
    }
  });

  onDestroy(() => {
    paletteStore.destroy();
    detachThemeSchemeListener();
  });

  const closeMobile = () => {
    mobileOpen = false;
  };

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
  <title>PMA Hub</title>
</svelte:head>

<div class:sidebar-collapsed={collapsed} class:mobile-open={mobileOpen} class="app-shell">
  <aside class="sidebar" aria-label="Primary navigation">
    <div class="brand-row">
      <a class="brand-mark" href={href('/chats')} onclick={closeMobile} aria-label="PMA Hub home">
        <span class="brand-glyph">P</span>
        <span class="brand-copy">
          <span class="brand-title">PMA Hub</span>
          <span class="brand-subtitle">Chats</span>
        </span>
      </a>
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
          </a>
        {/each}
      </div>
    </nav>

    <div class="sidebar-footer">
      <span class="status-dot" aria-hidden="true"></span>
      <span class="sidebar-footer-copy">OSS local mode</span>
    </div>
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
      <div class="hub-status" role="status" title="Hub ready" aria-label="Hub ready">
        <span class="status-dot" aria-hidden="true"></span>
      </div>
    </header>

    <main class="content-shell">
      {@render children()}
    </main>
  </div>
</div>

<Palette store={paletteStore} />
