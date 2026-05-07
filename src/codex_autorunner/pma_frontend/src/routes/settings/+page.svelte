<script lang="ts">
  import { onMount } from 'svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import SettingsView from '$lib/components/SettingsView.svelte';
  import {
    buildSessionUpdatePayload,
    buildSettingsViewModel,
    type SettingsSessionState,
    type SettingsViewModel
  } from '$lib/viewModels/settings';

  let view = $state<SettingsViewModel | null>(null);
  let sessionBaselineEpoch = $state(0);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let saveError = $state<ApiError | null>(null);
  let saving = $state(false);

  onMount(() => {
    void loadSettings();
  });

  async function loadSettings(): Promise<void> {
    loading = true;
    error = null;
    saveError = null;
    const [session, agents, files] = await Promise.all([
      pmaApi.settings.getSession(),
      pmaApi.pma.listAgents(),
      pmaApi.pma.listFiles()
    ]);

    if (!session.ok && !agents.ok && !files.ok) {
      error = session.error;
      loading = false;
      return;
    }

    const agentRows = agents.ok ? agents.data : [];
    const modelCatalogs: Record<string, JsonRecord[] | null> = {};
    await Promise.all(
      agentRows.map(async (agent) => {
        const agentId = stringField(agent, 'id');
        if (!agentId || !capabilityAllowed(agent, 'list_models')) return;
        const result = await pmaApi.pma.listAgentModels(agentId);
        modelCatalogs[agentId] = result.ok ? result.data : null;
      })
    );

    view = buildSettingsViewModel({
      session: session.ok ? session.data : null,
      agents: agentRows,
      modelCatalogs,
      fileArtifacts: files.ok ? files.data : []
    });
    sessionBaselineEpoch += 1;
    loading = false;
  }

  function updateSession(session: SettingsSessionState): void {
    if (!view) return;
    view = { ...view, session };
  }

  async function savePreferences(): Promise<void> {
    if (!view || saving) return;
    saving = true;
    saveError = null;
    const result = await pmaApi.settings.updateSession(buildSessionUpdatePayload(view.session));
    if (!result.ok) {
      saveError = result.error;
      saving = false;
      return;
    }
    await loadSettings();
    saving = false;
  }

  function stringField(record: JsonRecord, key: string): string | null {
    const value = record[key];
    return typeof value === 'string' && value.trim() ? value : null;
  }

  function capabilityAllowed(record: JsonRecord, action: string): boolean {
    const projection = record.capability_projection;
    if (!projection || typeof projection !== 'object') return false;
    const actions = (projection as JsonRecord).actions;
    if (!actions || typeof actions !== 'object') return false;
    const result = (actions as JsonRecord)[action];
    return Boolean(result && typeof result === 'object' && (result as JsonRecord).allowed === true);
  }
</script>

<SettingsView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {sessionBaselineEpoch}
  {view}
  errorMessage={error?.message ?? null}
  saveError={saveError?.message ?? null}
  {saving}
  onSessionChange={updateSession}
  onSavePreferences={savePreferences}
/>
