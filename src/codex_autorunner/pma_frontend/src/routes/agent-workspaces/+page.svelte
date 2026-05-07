<script lang="ts">
  import { onMount } from 'svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { AgentWorkspaceSummary } from '$lib/viewModels/domain';
  import { agentWorkspaceRoute } from '$lib/viewModels/routes';

  let workspaces = $state<AgentWorkspaceSummary[]>([]);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadAgentWorkspaces();
  });

  async function loadAgentWorkspaces(): Promise<void> {
    loading = true;
    error = null;
    const result = await pmaApi.hub.listAgentWorkspaces();
    if (!result.ok) {
      error = result.error;
      loading = false;
      return;
    }
    workspaces = result.data;
    loading = false;
  }
</script>

<section class="page-stack">
  <header class="page-hero">
    <span class="eyebrow">Agent scope</span>
    <h1>Agent workspaces</h1>
    <p>Backend-owned PMA workspaces available as chat scopes.</p>
  </header>

  {#if loading}
    <div class="state-panel">Loading agent workspaces...</div>
  {:else if error}
    <div class="state-panel error">Could not load agent workspaces. {error.message}</div>
  {:else}
    <div class="page-panel">
      {#if workspaces.length}
        <div class="dashboard-list">
          {#each workspaces as workspace}
            <a class="dashboard-row" href={href(agentWorkspaceRoute(workspace.id))}>
              <span>
                <strong>{workspace.name}</strong>
                <small>{workspace.runtime || workspace.resourceKind}</small>
              </span>
              <span class="status-pill">{workspace.enabled && workspace.existsOnDisk ? 'Ready' : 'Unavailable'}</span>
            </a>
          {/each}
        </div>
      {:else}
        <div class="state-panel">No agent workspaces registered.</div>
      {/if}
    </div>
  {/if}
</section>
