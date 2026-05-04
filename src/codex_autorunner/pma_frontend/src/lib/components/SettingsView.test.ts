import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import SettingsView from './SettingsView.svelte';
import { buildSettingsViewModel, type SettingsSensitiveAction } from '$lib/viewModels/settings';

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
      description: 'Remove repo/worktree state.',
      risk: 'high',
      action: 'delete_worktree',
      createdAt: null,
      raw: { target_scope: 'repo/main' }
    }
  ]
});

describe('settings page component', () => {
  it('renders the required settings sections with useful status', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('Hub');
    expect(body).toContain('PMA agent/model');
    expect(body).toContain('Coding agents');
    expect(body).toContain('Integrations');
    expect(body).toContain('Filebox/attachments');
    expect(body).toContain('Secrets');
    expect(body).toContain('Advanced/debug');
    expect(body).toContain('Hermes');
    expect(body).toContain('1 models');
    expect(body).toContain('Model listing unsupported');
    expect(body).toContain('Unavailable in PMA settings');
    expect(body).not.toContain('Analytics');
  });

  it('shows sensitive approvals without making normal coding permissions scary', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('Sensitive CAR approval');
    expect(body).toContain('delete_worktree');
    expect(body).toContain('PMA has full permission for normal coding work');
    expect(body).not.toContain('Approve code edits');
  });

  it('renders the explicit approval modal for sensitive settings actions', () => {
    const pendingAction: SettingsSensitiveAction = {
      id: 'modify-car-config',
      label: 'Modify CAR config',
      description: 'Change persistent hub settings.',
      available: true,
      reason: 'This writes control-plane state.'
    };

    const { body } = render(SettingsView, {
      props: {
        state: 'ready',
        view,
        pendingAction
      }
    });

    expect(body).toContain('role="dialog"');
    expect(body).toContain('Sensitive settings approval');
    expect(body).toContain('Modify CAR config');
    expect(body).toContain('Approve change');
    expect(body).toContain('Cancel');
  });
});
