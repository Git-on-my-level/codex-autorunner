<script lang="ts">
  export type ScopeChipNavItem = {
    label: string;
    href: string | null;
    current?: boolean;
  };

  let {
    label,
    detail,
    href = null,
    navItems = []
  }: {
    label: string;
    detail: string;
    href?: string | null;
    navItems?: ScopeChipNavItem[];
  } = $props();

  const visibleNavItems = $derived(navItems.filter((item) => item.href || item.current));
</script>

<div class="scope-chip" aria-label="Active route scope">
  <span class="scope-chip-copy">
    <span class="scope-chip-detail">{detail}</span>
    {#if href}
      <a class="scope-chip-label" href={href}>{label}</a>
    {:else}
      <span class="scope-chip-label">{label}</span>
    {/if}
  </span>
  {#if visibleNavItems.length > 0}
    <span class="scope-chip-nav" aria-label="Compatible scope navigation">
      {#each visibleNavItems as item (item.label)}
        {#if item.current || !item.href}
          <span class:current={item.current}>{item.label}</span>
        {:else}
          <a href={item.href}>{item.label}</a>
        {/if}
      {/each}
    </span>
  {/if}
</div>
