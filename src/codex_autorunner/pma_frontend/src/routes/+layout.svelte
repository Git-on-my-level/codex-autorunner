<script lang="ts">
  import { page } from '$app/state';
  import { primaryNav, navGroupLabels, isActiveRoute } from '$lib/navigation';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { Snippet } from 'svelte';
  import '../app.css';

  let { children }: { children: Snippet } = $props();
  let collapsed = $state(false);
  let mobileOpen = $state(false);
  const currentPath = $derived(stripRuntimeBasePath(page.url.pathname));
  const activeNavItem = $derived(primaryNav.find((item) => isActiveRoute(currentPath, item.href)) ?? primaryNav[0]);
  const activeGroupLabel = $derived(groupLabelForPath(currentPath, activeNavItem.group));
  const topbarTitle = $derived(titleForPath(currentPath, activeNavItem.label));

  const closeMobile = () => {
    mobileOpen = false;
  };

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') closeMobile();
  }

  function titleForPath(path: string, fallback: string): string {
    if (/^\/repos\/[^/]+\/tickets\/[^/]+/.test(path)) return 'Repo ticket';
    if (/^\/repos\/[^/]+\/tickets/.test(path)) return 'Repo tickets';
    if (/^\/worktrees\/[^/]+\/tickets\/[^/]+/.test(path)) return 'Worktree ticket';
    if (/^\/worktrees\/[^/]+\/tickets/.test(path)) return 'Worktree tickets';
    if (/^\/tickets\/[^/]+/.test(path)) return 'Ticket detail';
    return fallback;
  }

  function groupLabelForPath(path: string, fallback: keyof typeof navGroupLabels): string {
    if (path.startsWith('/worktrees/')) return navGroupLabels.support;
    return navGroupLabels[fallback];
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<svelte:head>
  <title>PMA Hub</title>
</svelte:head>

<div class:sidebar-collapsed={collapsed} class:mobile-open={mobileOpen} class="app-shell">
  <aside class="sidebar" aria-label="Primary navigation">
    <div class="brand-row">
      <a class="brand-mark" href={href('/pma')} onclick={closeMobile} aria-label="PMA Hub home">
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
        <span aria-hidden="true">{collapsed ? '›' : '‹'}</span>
      </button>
    </div>

    <nav class="nav-list">
      {#each Object.entries(navGroupLabels) as [group, groupLabel]}
        <div class="nav-group" aria-label={groupLabel}>
          <span class="nav-group-label">{groupLabel}</span>
          {#each primaryNav.filter((item) => item.group === group) as item}
            <a
              class:active={isActiveRoute(stripRuntimeBasePath(page.url.pathname), item.href)}
              class:indented={item.indent}
              class="nav-link"
              href={href(item.href)}
              onclick={closeMobile}
            >
              <span class="nav-initial" aria-hidden="true">{item.label.slice(0, 1)}</span>
              <span class="nav-label">{item.label}</span>
              {#if item.badge}
                <span class="nav-badge">{item.badge}</span>
              {/if}
            </a>
          {/each}
        </div>
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
        <span aria-hidden="true">≡</span>
      </button>
      <div class="topbar-copy">
        <span class="topbar-eyebrow">{activeGroupLabel}</span>
        <span class="topbar-title">{topbarTitle}</span>
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
