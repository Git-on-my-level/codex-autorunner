<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { AgentWorkspaceSummary } from '$lib/viewModels/domain';

  const workspaceId = $derived(page.params.workspaceId ?? 'unknown-agent-workspace');
  let workspace = $state<AgentWorkspaceSummary | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadAgentWorkspace();
  });

  async function loadAgentWorkspace(): Promise<void> {
    loading = true;
    error = null;
    const result = await pmaApi.hub.listAgentWorkspaces();
    if (!result.ok) {
      error = result.error;
      loading = false;
      return;
    }
    workspace = result.data.find((item) => item.id === workspaceId) ?? null;
    loading = false;
  }
</script>

<section class="page-stack">
  {#if loading}
    <div class="state-panel">Loading agent workspace...</div>
  {:else if error}
    <div class="state-panel error">Could not load agent workspace. {error.message}</div>
  {:else}
    <header class="page-hero">
      <span class="eyebrow">Agent workspace</span>
      <h1>{workspace?.name ?? workspaceId}</h1>
      <p>{workspace?.path ?? 'This agent workspace is registered as a PMA chat scope.'}</p>
      <div class="hero-actions">
        <a class="hero-action" href={href(`/chats?new=agent_workspace:${encodeURIComponent(workspaceId)}`)}>Start chat</a>
        <a class="hero-action" href={href('/agent-workspaces')}>All agent workspaces</a>
      </div>
    </header>
  {/if}
</section>
