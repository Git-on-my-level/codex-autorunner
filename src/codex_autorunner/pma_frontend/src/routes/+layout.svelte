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
  const breadcrumbs = $derived(breadcrumbsForPath(currentPath, activeNavItem.label, activeNavItem.group));

  const closeMobile = () => {
    mobileOpen = false;
  };

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') closeMobile();
  }

  function breadcrumbsForPath(path: string, fallback: string, fallbackGroup: keyof typeof navGroupLabels): { label: string; href: string | null }[] {
    const repoTicket = path.match(/^\/repos\/([^/]+)\/tickets\/([^/]+)/);
    if (repoTicket) {
      const repoId = decodeURIComponent(repoTicket[1]);
      const ticketId = decodeURIComponent(repoTicket[2]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Tickets', href: `/repos/${encodeURIComponent(repoId)}/tickets` },
        { label: `#${ticketId}`, href: null }
      ];
    }
    const repoTickets = path.match(/^\/repos\/([^/]+)\/tickets/);
    if (repoTickets) {
      const repoId = decodeURIComponent(repoTickets[1]);
      return [
        { label: 'Repos', href: '/repos' },
        { label: repoId, href: `/repos/${encodeURIComponent(repoId)}` },
        { label: 'Tickets', href: null }
      ];
    }
    const worktreeTicket = path.match(/^\/worktrees\/([^/]+)\/tickets\/([^/]+)/);
    if (worktreeTicket) {
      const worktreeId = decodeURIComponent(worktreeTicket[1]);
      const ticketId = decodeURIComponent(worktreeTicket[2]);
      return [
        { label: 'Worktrees', href: '/worktrees' },
        { label: worktreeId, href: `/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: `/worktrees/${encodeURIComponent(worktreeId)}/tickets` },
        { label: `#${ticketId}`, href: null }
      ];
    }
    const worktreeTickets = path.match(/^\/worktrees\/([^/]+)\/tickets/);
    if (worktreeTickets) {
      const worktreeId = decodeURIComponent(worktreeTickets[1]);
      return [
        { label: 'Worktrees', href: '/worktrees' },
        { label: worktreeId, href: `/worktrees/${encodeURIComponent(worktreeId)}` },
        { label: 'Tickets', href: null }
      ];
    }
    if (/^\/tickets\/[^/]+/.test(path)) return [{ label: 'All tickets', href: '/tickets' }, { label: 'Ticket detail', href: null }];
    return [{ label: path.startsWith('/worktrees/') ? navGroupLabels.support : navGroupLabels[fallbackGroup], href: null }, { label: fallback, href: null }];
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
      <nav class="topbar-copy" aria-label="Breadcrumb">
        {#each breadcrumbs as crumb, index}
          {#if crumb.href && index < breadcrumbs.length - 1}
            <a class="topbar-crumb" href={href(crumb.href)}>{crumb.label}</a>
          {:else}
            <span class="topbar-crumb current">{crumb.label}</span>
          {/if}
        {/each}
      </nav>
      <div class="hub-status" role="status">
        <span class="status-dot" aria-hidden="true"></span>
        <span>Hub ready</span>
      </div>
    </header>

    <main class={`content-shell ${currentPath === '/pma-memory' ? 'pinned-doc-content' : ''}`}>
      {@render children()}
    </main>
  </div>
</div>
