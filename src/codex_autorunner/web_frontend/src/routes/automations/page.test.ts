import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './[[ruleId]]/+page.svelte';

describe('/automations page', () => {
  function pageSource(): string {
    return readFileSync(fileURLToPath(new URL('./[[ruleId]]/+page.svelte', import.meta.url)), 'utf8');
  }

  it('renders the automation workspace shell before client automation data resolves', () => {
    const { body } = render(Page);

    expect(body).toContain('Automations workspace');
    expect(body).toContain('skeleton-chat-list');
    expect(body).toContain('skeleton-detail-hero');
    expect(body).not.toContain('This is a template');
  });

  it('uses typed product projections for managed segregation, schedules, messages, and raw diagnostics', () => {
    const source = pageSource();

    expect(source).toContain('const userAutomations = $derived(automations.filter((automation) => !isManagedAutomation(automation)))');
    expect(source).toContain('const managedAutomations = $derived(automations.filter(isManagedAutomation))');
    expect(source).toContain('Managed &amp; legacy diagnostics');
    expect(source).toContain("selectedAutomation()?.product.scheduleEditor.kind");
    expect(source).toContain("selectedKind === 'automation' && selectedScheduleKind() === 'one_shot'");
    expect(source).toContain("selectedKind === 'automation' && selectedScheduleKind() === 'interval'");
    expect(source).toContain('selectedAutomation()?.product.messageSource');
    expect(source).toContain('selectedMessagePreview()');
    expect(source).toContain('Diagnostic raw inspection');
    expect(source).toContain('selectedAutomation()?.product.rawLinks');
    expect(source).not.toContain("onblur={() => void saveJsonField('trigger'");
    expect(source).not.toContain("onblur={() => void saveJsonField('executor'");
    expect(source).not.toContain("onblur={() => void saveJsonField('policy'");
  });

  it('derives preset cards and drafts from the backend overview descriptors', () => {
    const source = pageSource();

    expect(source).toContain('const presets = $derived(overview?.presets ?? [])');
    expect(source).toContain('{#each presets as preset}');
    expect(source).toContain('renderPresetTemplate(preset.promptTemplate, selectedRepoId)');
    expect(source).toContain('preset.ticketBodyTemplate ? renderPresetTemplate(preset.ticketBodyTemplate, selectedRepoId) :');
    expect(source).not.toContain('const PRESETS');
  });

  it('loads the workspace index before hydrating detail, repo, and agent controls', () => {
    const source = pageSource();

    expect(source).toContain('webApi.hub.getAutomationWorkspaceIndex()');
    expect(source).toContain('webApi.hub.getAutomation(ruleId)');
    expect(source).toContain('webApi.hub.getAutomationTargetOptions()');
    expect(source).toContain("const presetTargetOptions = $derived(targetOptions.filter((option) => option.kind === 'repo'))");
    expect(source).toContain('{#each presetTargetOptions as repo}');
    expect(source).toContain('void hydrateAgentCatalog()');
    expect(source).toContain('Loading agent controls');
    expect(source).not.toContain('webApi.hub.listRepos()');
    expect(source).not.toContain('Promise.all([');
  });

  it('keeps edit-with-PMA drafts minimal and points PMA at the stored automation data', () => {
    const source = pageSource();
    const promptBody = source.match(
      /function editWithPmaPrompt\(\): string \{[\s\S]*?\n  function openPmaWithDraft/
    )?.[0] ?? '';

    expect(promptBody).toContain('Please edit CAR automation ${automation.id}.');
    expect(promptBody).toContain('Read the existing automation rule and related run history from the hub automation data before making changes.');
    expect(promptBody).toContain('<describe the change you want>');
    expect(promptBody).not.toContain('Current prompt or ticket body:');
    expect(promptBody).not.toContain('promptDraft || ticketDraft');
  });

  it('renders explicit runtime and effective run state copy', () => {
    const source = pageSource();

    expect(source).toContain('<dt>Agent</dt>');
    expect(source).toContain('<dt>Coordinator</dt>');
    expect(source).toContain('<dt>Workers</dt>');
    expect(source).toContain('Run with agent');
    expect(source).toContain('Run with PMA');
    expect(source).toContain('lastJob?.effectiveState');
    expect(source).toContain('raw-state-diagnostic');
    expect(source).toContain('blocked-state-diagnostic');
    expect(source).toContain('policy-state-diagnostic');
    expect(source).not.toContain('<dt>Runs as</dt>');
  });
});
