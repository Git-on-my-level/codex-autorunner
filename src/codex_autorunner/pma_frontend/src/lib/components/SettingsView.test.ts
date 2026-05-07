import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import SettingsView from './SettingsView.svelte';
import { buildSettingsViewModel } from '$lib/viewModels/settings';

const projection = (agent_id: string, allowed: boolean, reason: string | null = null) => ({
  agent_id,
  actions: {
    list_models: { allowed, missing_capabilities: allowed ? [] : ['model_listing'], reason }
  }
});

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
      capability_projection: projection('codex', false, 'Model listing unsupported')
    }
  ],
  modelCatalogs: {
    hermes: [{ id: 'gpt-5.4' }]
  }
});

describe('settings page component', () => {
  it('renders the required settings sections with useful status', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('Hub');
    expect(body).toContain('PMA agent');
    expect(body).toContain('Coding agents');
    expect(body).toContain('Integrations');
    expect(body).toContain('Attachments');
    expect(body).toContain('Secrets');
    expect(body).toContain('Advanced');
    expect(body).toContain('Hermes');
    expect(body).toContain('1 models');
    expect(body).toContain('Model listing unsupported');
    expect(body).toContain('Unavailable in PMA settings');
    expect(body).toContain('Approval policy');
    expect(body).toContain('Sandbox mode');
    expect(body).toContain('Workspace-write network');
    expect(body).toContain('Agent-native approvals apply during agent turns');
    expect(body).not.toContain('Analytics');
  });

  it('does not render special PMA approval prompts for settings updates', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).not.toContain('Sensitive CAR approval');
    expect(body).not.toContain('Request approval');
    expect(body).not.toContain('Pending approvals');
    expect(body).toContain('Saved');
    expect(body).not.toContain('Approve code edits');
  });
});
