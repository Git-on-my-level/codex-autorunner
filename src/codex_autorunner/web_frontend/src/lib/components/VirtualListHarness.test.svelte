<script lang="ts">
  import VirtualList from './VirtualList.svelte';

let { count = 5000, initialCount = 40 } = $props<{ count?: number; initialCount?: number }>();
const rows = $derived(Array.from({ length: count }, (_, index) => ({
  id: `row-${index + 1}`,
  label: `Row ${index + 1}`
})));
</script>

<VirtualList
  items={rows}
  key={(row) => row.id}
  estimatedItemSize={32}
  {initialCount}
  ariaLabel="Seeded rows"
>
  {#snippet children(row, index)}
    <button type="button" class="seeded-row" aria-label={`Open ${row.label}`}>
      {index + 1}: {row.label}
    </button>
  {/snippet}
</VirtualList>
