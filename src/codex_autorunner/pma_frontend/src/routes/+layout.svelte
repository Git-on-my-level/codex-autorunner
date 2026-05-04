<script lang="ts">
  import { page } from '$app/state';
  import { primaryNav, isActiveRoute } from '$lib/navigation';
  import type { Snippet } from 'svelte';
  import '../app.css';

  let { children }: { children: Snippet } = $props();
  let collapsed = $state(false);
  let mobileOpen = $state(false);

  const closeMobile = () => {
    mobileOpen = false;
  };

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') closeMobile();
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<svelte:head>
  <title>PMA Hub</title>
</svelte:head>

<div class:sidebar-collapsed={collapsed} class:mobile-open={mobileOpen} class="app-shell">
  <aside class="sidebar" aria-label="Primary navigation">
    <div class="brand-row">
      <a class="brand-mark" href="/pma" onclick={closeMobile} aria-label="PMA Hub home">
        <span class="brand-glyph">P</span>
        <span class="brand-copy">
          <span class="brand-title">PMA Hub</span>
          <span class="brand-subtitle">Local workspace</span>
        </span>
      </a>
      <button
        class="icon-button desktop-collapse"
        type="button"
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        onclick={() => (collapsed = !collapsed)}
      >
        <span aria-hidden="true">{collapsed ? '>' : '<'}</span>
      </button>
    </div>

    <nav class="nav-list">
      {#each primaryNav as item}
        <a
          class:active={isActiveRoute(page.url.pathname, item.href)}
          class="nav-link"
          href={item.href}
          onclick={closeMobile}
        >
          <span class="nav-initial" aria-hidden="true">{item.label.slice(0, 1)}</span>
          <span class="nav-label">{item.label}</span>
          {#if item.badge}
            <span class="nav-badge">{item.badge}</span>
          {/if}
        </a>
      {/each}
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
        <span aria-hidden="true">=</span>
      </button>
      <div class="topbar-copy">
        <span class="topbar-eyebrow">PMA</span>
        <span class="topbar-title">Chat-first control plane</span>
      </div>
      <div class="hub-status" role="status">
        <span class="status-dot" aria-hidden="true"></span>
        <span>Hub ready</span>
      </div>
    </header>

    <main class="content-shell">
      {@render children()}
    </main>
  </div>
</div>
