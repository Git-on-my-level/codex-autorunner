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
    expect(body).toContain('Loading automations');
    expect(body).toContain('Diagnostic raw inspection');
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

  it('loads the workspace read model before hydrating repo and agent controls', () => {
    const source = pageSource();

    expect(source).toContain('webApi.hub.getAutomationWorkspace()');
    expect(source).toContain('targetOptions = automationResult.data.targetOptions');
    expect(source).toContain("const presetTargetOptions = $derived(targetOptions.filter((option) => option.kind === 'repo'))");
    expect(source).toContain('{#each presetTargetOptions as repo}');
    expect(source).toContain('void hydrateAgentCatalog()');
    expect(source).toContain('Loading agent controls');
    expect(source).not.toContain('webApi.hub.listRepos()');
    expect(source).not.toContain('Promise.all([');
  });
});
