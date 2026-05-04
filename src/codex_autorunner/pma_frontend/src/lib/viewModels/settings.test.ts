import { describe, expect, it } from 'vitest';
import { buildSessionUpdatePayload, buildSettingsViewModel } from './settings';

describe('settings view model', () => {
  it('groups settings sections around wired status and unavailable sensitive controls', () => {
    const view = buildSettingsViewModel({
      session: {
        autorunner_model_override: 'gpt-5.4',
        autorunner_effort_override: 'medium',
        autorunner_approval_policy: 'never',
        autorunner_sandbox_mode: 'dangerFullAccess'
      },
      agents: [
        { id: 'hermes', name: 'Hermes', capabilities: ['durable_threads', 'model_listing'] },
        { id: 'codex', name: 'Codex', capabilities: ['message_turns'] }
      ],
      modelCatalogs: {
        hermes: [{ id: 'gpt-5.4' }]
      },
      approvals: [
        {
          id: 'delete-worktree',
          title: 'Delete worktree',
          description: 'Remove repo/worktree state',
          risk: 'high',
          action: 'delete_worktree',
          createdAt: null,
          raw: {}
        },
        {
          id: 'normal-run',
          title: 'Run tests',
          description: 'Normal coding work',
          risk: 'low',
          action: 'run_tests',
          createdAt: null,
          raw: {}
        }
      ]
    });

    expect(view.hub.map((item) => item.label)).toContain('Runtime settings API');
    expect(view.pmaAgents).toMatchObject([{ id: 'hermes', modelStatus: 'available', modelCount: 1 }]);
    expect(view.codingAgents).toMatchObject([{ id: 'codex', modelStatus: 'unsupported' }]);
    expect(view.secrets[0]).toMatchObject({ value: 'Unavailable in PMA settings' });
    expect(view.sensitiveActions.every((action) => action.available === false)).toBe(true);
    expect(view.approvals).toHaveLength(1);
  });

  it('builds the narrow session settings payload without approval-heavy coding controls', () => {
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
      runner_stop_after_runs: 3
    });
  });
});
