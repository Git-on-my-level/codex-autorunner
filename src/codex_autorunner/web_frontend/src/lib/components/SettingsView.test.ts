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

const updateTargetProps = {
  updateTargets: [
    {
      value: 'all',
      label: 'all',
      description: 'Web + Telegram + Discord',
      includesWeb: true,
      restartNotice: 'The web UI, Telegram, and Discord will restart.'
    },
    {
      value: 'chat',
      label: 'chat',
      description: 'Telegram + Discord',
      includesWeb: false,
      restartNotice: 'Telegram and Discord will restart.'
    }
  ],
  selectedUpdateTarget: 'all',
  updateStatus: {
    status: 'idle',
    message: 'No update running.',
    at: null,
    phase: null,
    errorType: null,
    exitCode: null,
    updateRunId: null,
    updateTarget: null,
    raw: {}
  }
};

describe('settings page shell', () => {
  it('renders the section nav at the list level', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view } });

    expect(body).toContain('PMA memory');
    expect(body).toContain('General');
    expect(body).toContain('Integrations');
    expect(body).toContain('Agents &amp; Runner');
    // List view doesn't render section content until a section is selected.
    expect(body).not.toContain('Color theme');
    expect(body).not.toContain('Reasoning override');
  });

  it('renders appearance + system controls in the general section', () => {
    const { body } = render(SettingsView, {
      props: { state: 'ready', view, sectionId: 'general', ...updateTargetProps }
    });

    expect(body).toContain('Appearance');
    expect(body).toContain('Color theme');
    expect(body).toContain('System (match OS)');
    expect(body).toContain('System update');
    expect(body).toContain('Update target');
    expect(body).toContain('Start update');
    expect(body).toContain('Refresh status');
    expect(body).toContain('Web + Telegram + Discord');
    expect(body).toContain('No update running.');
  });

  it('renders integration setup cards when integrations is selected', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view, sectionId: 'integrations' } });

    expect(body).toContain('Setup with PMA');
    expect(body).toContain('Telegram');
    expect(body).toContain('Discord');
    expect(body).toContain('Notifications');
    expect(body).toContain('GitHub automation');
    expect(body).toContain('Voice transcription');
    expect(body).toContain('Enable with PMA');
  });

  it('renders runner overrides and agents when agents section is selected', () => {
    const { body } = render(SettingsView, { props: { state: 'ready', view, sectionId: 'agents' } });

    expect(body).toContain('Runner overrides');
    expect(body).toContain('Reasoning override');
    expect(body).toContain('Approval policy');
    expect(body).toContain('Sandbox mode');
    expect(body).toContain('Workspace-write network');
    expect(body).toContain('Ticket flow commits');
    expect(body).toContain('Require a git commit before advancing after a completed ticket');
    expect(body).toContain('Hermes');
    expect(body).toContain('Codex');
    expect(body).toContain('Default model');
    expect(body).toContain('Use built-in default');
    expect(body).toContain('Model selection unavailable');
    expect(body).toContain('Saved');
    expect(body).not.toContain('6 models');
    expect(body).not.toContain('Sensitive CAR approval');
    expect(body).not.toContain('Approve code edits');
  });

  it('does not leak removed legacy panels into any section', () => {
    for (const sectionId of ['general', 'integrations', 'agents'] as const) {
      const { body } = render(SettingsView, {
        props: { state: 'ready', view, sectionId, ...updateTargetProps }
      });
      expect(body).not.toContain('Direct save');
      expect(body).not.toContain('Hub mode');
      expect(body).not.toContain('Runtime settings API');
      expect(body).not.toContain('Secrets');
      expect(body).not.toContain('External integrations');
      expect(body).not.toContain('Attachments');
      expect(body).not.toContain('Agent-native approvals apply during agent turns');
      expect(body).not.toContain('Analytics');
    }
  });
});
