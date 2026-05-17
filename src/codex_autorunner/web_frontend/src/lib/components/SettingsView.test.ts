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
    autorunner_model_overrides: { codex: 'gpt-5.4' },
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
    hermes: [{ id: 'gpt-5.4', display_name: 'GPT-5.4' }]
  }
});

describe('settings page component', () => {
  it('renders appearance theme controls', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('Appearance');
    expect(body).toContain('Color theme');
    expect(body).toContain('Dracula');
    expect(body).toContain('Solarized Light');
    expect(body).toContain('System (match OS)');
  });

  it('renders the required settings sections with useful status', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('PMA memory');
    expect(body).toContain('Setup with PMA');
    expect(body).toContain('Telegram');
    expect(body).toContain('Discord');
    expect(body).toContain('Notifications');
    expect(body).toContain('GitHub automation');
    expect(body).toContain('Runner overrides');
    expect(body).toContain('Agents');
    expect(body).toContain('Hermes');
    expect(body).toContain('Codex');
    expect(body).toContain('Default model');
    expect(body).toContain('Use built-in default');
    expect(body).toContain('GPT-5.4 (gpt-5.4)');
    expect(body).toContain('Model selection unavailable');
    expect(body).not.toContain('no listing');
    expect(body).not.toContain('6 models');
    expect(body).not.toContain('20 models');
    expect(body).toContain('Reasoning override');
    expect(body).toContain('Approval policy');
    expect(body).toContain('Sandbox mode');
    expect(body).toContain('Workspace-write network');
    expect(body).toContain('Ticket flow commits');
    expect(body).toContain('Require a git commit before advancing after a completed ticket');
    expect(body).toContain('Voice transcription');
    expect(body).toContain('Enable with PMA');
    // The flat redesign drops the noisy default-state hub status block, the
    // duplicated advanced/debug list, the empty Secrets section, and
    // boilerplate prose next to the form.
    expect(body).not.toContain('Direct save');
    expect(body).not.toContain('Hub mode');
    expect(body).not.toContain('Runtime settings API');
    expect(body).not.toContain('Secrets');
    expect(body).not.toContain('External integrations');
    expect(body).not.toContain('Coding');
    expect(body).not.toContain('Attachments');
    expect(body).not.toContain('Agent-native approvals apply during agent turns');
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
