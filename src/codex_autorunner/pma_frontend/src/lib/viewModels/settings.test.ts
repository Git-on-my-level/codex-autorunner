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
        autorunner_model_override: 'gpt-5.4',
        autorunner_effort_override: 'medium',
        autorunner_approval_policy: 'never',
        autorunner_sandbox_mode: 'dangerFullAccess',
        autorunner_workspace_write_network: true
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
        hermes: [{ id: 'gpt-5.4' }]
      }
    });

    expect(view.hub.map((item) => item.label)).toContain('Runtime settings API');
    expect(view.hub).toContainEqual({ label: 'Settings changes', value: 'Direct save', tone: 'ok' });
    expect(view.pmaAgents).toMatchObject([{ id: 'hermes', modelStatus: 'available', modelCount: 1 }]);
    expect(view.codingAgents).toMatchObject([{ id: 'codex', modelStatus: 'unsupported' }]);
    expect(view.secrets[0]).toMatchObject({ value: 'Unavailable in PMA settings' });
    expect(view.advanced).toContainEqual({ label: 'Approval policy', value: 'never', tone: 'muted' });
    expect(view.advanced).toContainEqual({ label: 'Sandbox mode', value: 'dangerFullAccess', tone: 'muted' });
    expect(view.advanced).toContainEqual({ label: 'Workspace-write network', value: 'Enabled', tone: 'muted' });
  });

  it('builds the full direct-save session settings payload', () => {
    expect(
      buildSessionUpdatePayload({
        modelOverride: 'gpt-5.4',
        effortOverride: '',
        stopAfterRuns: '3',
        approvalPolicy: 'never',
        sandboxMode: 'dangerFullAccess',
        workspaceWriteNetwork: null
      })
    ).toEqual({
      autorunner_model_override: 'gpt-5.4',
      autorunner_effort_override: null,
      autorunner_approval_policy: 'never',
      autorunner_sandbox_mode: 'dangerFullAccess',
      autorunner_workspace_write_network: null,
      runner_stop_after_runs: 3
    });
  });
});
