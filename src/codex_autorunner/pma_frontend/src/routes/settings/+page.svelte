<script lang="ts">
  import { onMount } from 'svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import SettingsView from '$lib/components/SettingsView.svelte';
  import {
    buildSessionUpdatePayload,
    buildSettingsViewModel,
    type SettingsSensitiveAction,
    type SettingsSessionState,
    type SettingsViewModel
  } from '$lib/viewModels/settings';
  import type { SensitiveApprovalRequest } from '$lib/viewModels/domain';
  import { approvalActionUrl, filterSensitiveCarApprovals } from '$lib/viewModels/pmaChat';

  let view = $state<SettingsViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let saveError = $state<ApiError | null>(null);
  let pendingAction = $state<SettingsSensitiveAction | null>(null);

  onMount(() => {
    void loadSettings();
  });

  async function loadSettings(): Promise<void> {
    loading = true;
    error = null;
    saveError = null;
    const [session, agents, files, approvals] = await Promise.all([
      pmaApi.settings.getSession(),
      pmaApi.pma.listAgents(),
      pmaApi.pma.listFiles(),
      pmaApi.settings.listApprovals()
    ]);

    if (!session.ok && !agents.ok && !files.ok && !approvals.ok) {
      error = session.error;
      loading = false;
      return;
    }

    const agentRows = agents.ok ? agents.data : [];
    const modelCatalogs: Record<string, JsonRecord[] | null> = {};
    await Promise.all(
      agentRows.map(async (agent) => {
        const agentId = stringField(agent, 'id');
        const capabilities = arrayField(agent, 'capabilities');
        if (!agentId || !capabilities.includes('model_listing')) return;
        const result = await pmaApi.pma.listAgentModels(agentId);
        modelCatalogs[agentId] = result.ok ? result.data : null;
      })
    );

    view = buildSettingsViewModel({
      session: session.ok ? session.data : null,
      agents: agentRows,
      modelCatalogs,
      fileArtifacts: files.ok ? files.data : [],
      approvals: approvals.ok ? approvals.data : []
    });
    loading = false;
  }

  function updateSession(session: SettingsSessionState): void {
    if (!view) return;
    view = { ...view, session };
  }

  async function confirmSensitiveAction(action: SettingsSensitiveAction): Promise<void> {
    pendingAction = null;
    saveError = null;
    if (action.id !== 'update-runtime-preferences' || !view) return;
    const result = await pmaApi.settings.updateSession(buildSessionUpdatePayload(view.session));
    if (!result.ok) {
      saveError = result.error;
      return;
    }
    view = buildSettingsViewModel({
      session: result.data,
      agents: [...view.pmaAgents, ...view.codingAgents].map((agent) => ({
        id: agent.id,
        name: agent.name,
        capabilities: agent.capabilities
      })),
      approvals: view.approvals
    });
    await loadSettings();
  }

  async function decideApproval(approval: SensitiveApprovalRequest, decision: 'approve' | 'decline'): Promise<void> {
    saveError = null;
    const url = approvalActionUrl(approval, decision);
    if (!url) {
      saveError = {
        kind: 'parse',
        status: null,
        code: 'approval_route_missing',
        message: 'This approval is visible, but the backend did not expose an approve/decline route.'
      };
      return;
    }
    const body = url === approval.raw.decision_url || url === approval.raw.route ? { decision, approval_id: approval.id } : undefined;
    const result = await pmaApi.requestJson<JsonRecord>(url, { method: 'POST', body });
    if (!result.ok) {
      saveError = result.error;
      return;
    }
    if (view) view = { ...view, approvals: filterSensitiveCarApprovals(view.approvals.filter((item) => item.id !== approval.id)) };
    void loadSettings();
  }

  function stringField(record: JsonRecord, key: string): string | null {
    const value = record[key];
    return typeof value === 'string' && value.trim() ? value : null;
  }

  function arrayField(record: JsonRecord, key: string): string[] {
    const value = record[key];
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  }
</script>

<SettingsView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {view}
  errorMessage={error?.message ?? null}
  saveError={saveError?.message ?? null}
  {pendingAction}
  onSessionChange={updateSession}
  onRequestSensitiveAction={(action) => (pendingAction = action)}
  onConfirmSensitiveAction={confirmSensitiveAction}
  onCancelSensitiveAction={() => (pendingAction = null)}
  onApprovalDecision={decideApproval}
/>
