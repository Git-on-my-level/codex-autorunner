import { describe, expect, it } from 'vitest';
import { buildSessionUpdatePayload, buildSettingsViewModel } from './settings';

describe('settings view model', () => {
  const projection = (agent_id: string, allowed: boolean, reason: string | null = null) => ({
    agent_id,
    actions: {
      list_models: { allowed, missing_capabilities: allowed ? [] : ['model_listing'], reason }
    }
  });

  it('groups settings sections around wired status and direct settings saves', () => {
    const view = buildSettingsViewModel({
      session: {
        autorunner_model_overrides: { hermes: 'hermes-model' },
        autorunner_effort_override: 'medium',
        autorunner_approval_policy: 'never',
        autorunner_sandbox_mode: 'dangerFullAccess',
        autorunner_workspace_write_network: true,
        ticket_flow_require_commit: false
      },
      agents: [
        { id: 'hermes', name: 'Hermes', capabilities: ['durable_threads', 'model_listing'], capability_projection: projection('hermes', true) },
        {
          id: 'codex',
          name: 'Codex',
          capabilities: ['message_turns'],
          capability_projection: projection('codex', false, 'Cannot list models; missing capability: model_listing')
        }
      ],
      modelCatalogs: {
        hermes: [{ id: 'gpt-5.4', display_name: 'GPT-5.4' }]
      }
    });

    expect(view.hub.map((item) => item.label)).toContain('Runtime settings API');
    expect(view.session.modelOverrides).toEqual({ hermes: 'hermes-model' });
    expect(view.session.ticketFlowRequireCommit).toBe(false);
    expect(view.hub).toContainEqual({ label: 'Settings changes', value: 'Direct save', tone: 'ok' });
    expect(view.agents).toMatchObject([
      { id: 'hermes', modelStatus: 'available', modelCount: 1, modelOptions: [{ id: 'gpt-5.4', label: 'GPT-5.4 (gpt-5.4)' }] },
      { id: 'codex', modelStatus: 'unsupported' }
    ]);
    expect(view.integrations).toContainEqual({
      label: 'Chat setup',
      value: 'Configure Discord, Telegram, and notifications with PMA guidance',
      tone: 'muted'
    });
    expect(view.secrets[0]).toMatchObject({ value: 'Unavailable in PMA settings' });
    expect(view.advanced).toContainEqual({ label: 'Approval policy', value: 'never', tone: 'muted' });
    expect(view.advanced).toContainEqual({ label: 'Sandbox mode', value: 'dangerFullAccess', tone: 'muted' });
    expect(view.advanced).toContainEqual({ label: 'Workspace-write network', value: 'Enabled', tone: 'muted' });
  });

  it('builds the full direct-save session settings payload', () => {
    expect(
      buildSessionUpdatePayload({
        modelOverrides: { codex: 'gpt-5.4', opencode: 'zai/default', hermes: '' },
        effortOverride: '',
        stopAfterRuns: '3',
        approvalPolicy: 'never',
        sandboxMode: 'dangerFullAccess',
        workspaceWriteNetwork: null,
        ticketFlowRequireCommit: false
      })
    ).toEqual({
      autorunner_model_overrides: { codex: 'gpt-5.4', opencode: 'zai/default' },
      autorunner_effort_override: null,
      autorunner_approval_policy: 'never',
      autorunner_sandbox_mode: 'dangerFullAccess',
      autorunner_workspace_write_network: null,
      ticket_flow_require_commit: false,
      runner_stop_after_runs: 3
    });
  });
});
