<script lang="ts">
  const summaries = [
    ['Active runs', '2'],
    ['Waiting for me', '0'],
    ['Failed', '0'],
    ['Open tickets', '12'],
    ['Repos', '5'],
    ['Worktrees', '8']
  ];
</script>

<section class="page-stack">
  <div class="section-heading">
    <p class="eyebrow">Overview</p>
    <h1>Dashboard</h1>
  </div>
  <div class="summary-grid">
    {#each summaries as [label, value]}
      <article class="metric-card">
        <span>{label}</span>
        <strong>{value}</strong>
      </article>
    {/each}
  </div>
  <div class="page-panel">
    <h2>Recent activity</h2>
    <p>Operational summaries will link back into PMA rooms, repos, worktrees, and tickets.</p>
  </div>
</section>
