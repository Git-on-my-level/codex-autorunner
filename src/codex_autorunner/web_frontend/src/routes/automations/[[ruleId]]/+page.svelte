<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import MasterDetail from '$lib/components/MasterDetail.svelte';
  import RunHistoryList from '$lib/components/tickets/RunHistoryList.svelte';
  import TicketPackEditor from '$lib/components/tickets/TicketPackEditor.svelte';
  import { confirmDialog } from '$lib/components/confirmDialog';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import { runHistoryFromAutomationJobs } from '$lib/viewModels/runHistory';
  import {
    webApi,
    type ApiError,
    type AutomationOverview,
    type AutomationPresetDescriptor,
    type AutomationSummary,
    type AutomationTargetOption,
    type AutomationUpdateRequest,
    type JsonRecord
  } from '$lib/api/client';
  import { resolveAgentModelSelection } from '$lib/viewModels/modelPickers';

  type PresetId = 'security_scan_pr' | 'weekly_ticket_flow';
  type SelectionKind = 'automation' | 'preset' | null;
  type JsonField = 'trigger' | 'filters' | 'target' | 'executor' | 'policy' | 'metadata';
  type TicketPackTicket = { path: string; content: string };

  let overview = $state<AutomationOverview | null>(null);
  let targetOptions = $state<AutomationTargetOption[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let loading = $state(true);
  let loadingTargetOptions = $state(false);
  let loadingAgents = $state(false);
  let loadingModels = $state(false);
  let detailLoadingId = $state<string | null>(null);
  let modelCatalogAgentId = $state('');
  let saving = $state(false);
  let actionId = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let detailError = $state<ApiError | null>(null);
  let notice = $state<string | null>(null);
  let selectedKind = $state<SelectionKind>(null);
  let selectedId = $state<string>('');
  let detailMode = $state<'list' | 'detail'>('list');
  let deleting = $state(false);
  let saveTimer: number | null = null;

  let selectedRepoId = $state('');
  let defaultAgentId = $state('');
  let defaultProfile = $state('');
  let selectedAgent = $state('');
  let selectedProfile = $state('');
  let selectedModel = $state('');
  let selectedReasoning = $state('');
  let detailExecutionMode = $state('agent_task_turn');
  let agentCatalogError = $state<string | null>(null);
  let modelCatalogError = $state<string | null>(null);
  let timezone = $state(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  let detailName = $state('');
  let detailEnabled = $state(false);
  let detailHour = $state(9);
  let detailMinute = $state(0);
  let detailWeekday = $state(0);
  let promptDraft = $state('');
  let ticketDraft = $state('');
  let triggerDraft = $state('');
  let filtersDraft = $state('');
  let targetDraft = $state('');
  let executorDraft = $state('');
  let policyDraft = $state('');
  let metadataDraft = $state('');
  let syncedSelectionKey = '';
  let hydratedDetailIds = $state<string[]>([]);
  let agentsHydrated = $state(false);

  const automations = $derived(overview?.automations ?? []);
  const presets = $derived(overview?.presets ?? []);
  const userAutomations = $derived(automations.filter((automation) => !isManagedAutomation(automation)));
  const managedAutomations = $derived(automations.filter(isManagedAutomation));
  const routeRuleId = $derived(decodeURIComponent(page.params.ruleId ?? ''));
  const presetTargetOptions = $derived(targetOptions.filter((option) => option.kind === 'repo'));
  const selectedRepo = $derived(targetOptions.find((repo) => repo.id === selectedRepoId) ?? null);
  const scheduleTimeValue = $derived(`${pad(detailHour)}:${pad(detailMinute)}`);
  const selectionResolved = $derived(
    selectedKind === null || selectedAutomation() !== null || selectedPreset() !== null
  );
  const selectedAutomationHydrated = $derived(
    selectedKind !== 'automation' || hydratedDetailIds.includes(selectedId)
  );
  const routeAutomationMissing = $derived(
    Boolean(routeRuleId && overview && !loading && !automations.some((automation) => automation.id === routeRuleId))
  );

  onMount(() => {
    void load();
    return () => {
      if (saveTimer) window.clearTimeout(saveTimer);
    };
  });

  async function load(): Promise<void> {
    loading = true;
    error = null;
    const automationResult = await webApi.hub.getAutomationWorkspaceIndex();
    if (automationResult.ok) {
      overview = mergeAutomationOverview(automationResult.data);
      if (!selectedRepoId) {
        selectedRepoId = presetTargetOptions.find((option) => !option.disabled)?.id ?? presetTargetOptions[0]?.id ?? '';
      }
      defaultAgentId = automationResult.data.agentDefaults.defaultAgent;
      defaultProfile = automationResult.data.agentDefaults.defaultProfile ?? '';
      selectedModel = selectedModel || automationResult.data.agentDefaults.defaultModel || '';
      selectedReasoning = selectedReasoning || automationResult.data.agentDefaults.defaultReasoning || '';
    }
    else error = automationResult.error;
    loading = false;
    syncDraftsForSelection(true);
    if (selectedKind === 'automation' && selectedId) void hydrateAutomationDetail(selectedId, { force: true });
    void hydrateTargetOptions();
  }

  async function hydrateAgentCatalog(): Promise<void> {
    if (agentsHydrated || loadingAgents) return;
    loadingAgents = true;
    agentCatalogError = null;
    const agentResult = await webApi.pma.listAgents();
    loadingAgents = false;
    if (agentResult.ok) {
      agents = agentResult.data.agents;
      defaultAgentId = agentResult.data.default;
      defaultProfile = String(agentResult.data.defaults.profile ?? '');
      agentsHydrated = true;
      syncDraftsForSelection(true);
      if (selectedExecutorKind() === 'agent_task_turn') void loadModels(selectedAgent, selectedModel, { keepReasoning: selectedKind === 'automation' });
    } else {
      agentCatalogError = agentResult.error.message;
    }
  }

  async function hydrateTargetOptions(): Promise<void> {
    if (targetOptions.length || loadingTargetOptions) return;
    loadingTargetOptions = true;
    const result = await webApi.hub.getAutomationTargetOptions();
    loadingTargetOptions = false;
    if (result.ok) {
      targetOptions = result.data;
      if (!selectedRepoId) {
        selectedRepoId = result.data.find((option) => option.kind === 'repo' && !option.disabled)?.id ?? result.data.find((option) => option.kind === 'repo')?.id ?? '';
        syncDraftsForSelection(true);
      }
    }
  }

  function mergeAutomationOverview(next: AutomationOverview): AutomationOverview {
    if (!overview || hydratedDetailIds.length === 0) return next;
    const currentById = new Map(overview.automations.map((automation) => [automation.id, automation]));
    return {
      ...next,
      automations: next.automations.map((automation) => {
        const current = currentById.get(automation.id);
        return current && hydratedDetailIds.includes(automation.id) ? current : automation;
      })
    };
  }

  async function hydrateAutomationDetail(ruleId: string, options: { force?: boolean } = {}): Promise<void> {
    if (!ruleId || (!options.force && hydratedDetailIds.includes(ruleId)) || detailLoadingId === ruleId) return;
    detailLoadingId = ruleId;
    detailError = null;
    const result = await webApi.hub.getAutomation(ruleId);
    if (detailLoadingId === ruleId) detailLoadingId = null;
    if (selectedKind !== 'automation' || selectedId !== ruleId) return;
    if (!result.ok) {
      detailError = result.error;
      if (!hydratedDetailIds.includes(ruleId)) hydratedDetailIds = [...hydratedDetailIds, ruleId];
      return;
    }
    replaceAutomation(result.data);
    if (!hydratedDetailIds.includes(ruleId)) hydratedDetailIds = [...hydratedDetailIds, ruleId];
    syncDraftsForSelection(true);
  }

  // The URL is the source of truth for which automation is open. Presets have no
  // route — they stay an in-memory selection on the bare /automations path.
  $effect(() => {
    const ruleId = routeRuleId;
    if (ruleId && automations.some((automation) => automation.id === ruleId)) {
      selectedKind = 'automation';
      selectedId = ruleId;
      detailMode = 'detail';
      void hydrateAutomationDetail(ruleId);
    } else if (ruleId && overview && !loading) {
      selectedKind = 'automation';
      selectedId = ruleId;
      detailMode = 'detail';
    } else if (!ruleId && selectedKind === 'automation') {
      // The selected automation was deleted or the URL was cleared — drop back to the list.
      selectedKind = null;
      selectedId = '';
      detailMode = 'list';
    } else if (!ruleId) {
      detailMode = selectedKind === 'preset' ? 'detail' : 'list';
    }
    syncDraftsForSelection(false);
  });

  $effect(() => {
    if (!selectionResolved || !selectedAutomationHydrated) return;
    if (selectedExecutorKind() === 'agent_task_turn' && canEditPrompt()) void hydrateAgentCatalog();
  });

  $effect(() => {
    if (!agentsHydrated || loadingModels) return;
    if (selectedExecutorKind() !== 'agent_task_turn' || !canEditPrompt()) return;
    if (selectedAgent && modelCatalogAgentId !== selectedAgent) {
      void loadModels(selectedAgent, selectedModel, { keepReasoning: selectedKind === 'automation' });
    }
  });

  function automationRoute(ruleId: string): string {
    return href(`/automations/${encodeURIComponent(ruleId)}`);
  }

  function selectedAutomation(): AutomationSummary | null {
    return selectedKind === 'automation' ? automations.find((automation) => automation.id === selectedId) ?? null : null;
  }

  function selectedPreset(): AutomationPresetDescriptor | null {
    if (selectedKind !== 'preset') return null;
    return presets.find((preset) => preset.id === selectedId) ?? null;
  }

  function renderPresetTemplate(template: string, repoId: string, values: JsonRecord = {}): string {
    return template
      .replaceAll('{repo_id}', repoId || 'selected repo')
      .replaceAll('{ticket_id}', 'tkt_<assigned on save>')
      .replaceAll('{automation_slug}', '<assigned on save>')
      .replaceAll('{prompt}', String(values.prompt ?? '<message>'))
      .replaceAll('{ticket_body}', String(values.ticket_body ?? '<ticket body>'));
  }

  function renderPresetValue(value: unknown, repoId: string, values: JsonRecord = {}): unknown {
    if (typeof value === 'string') return renderPresetTemplate(value, repoId, values);
    if (Array.isArray(value)) return value.map((item) => renderPresetValue(item, repoId, values));
    const record = asRecord(value);
    if (!Object.keys(record).length) return value;
    return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, renderPresetValue(item, repoId, values)]));
  }

  function selectionKey(): string {
    return `${selectedKind}:${selectedId}:${selectedAutomation()?.updatedAt ?? ''}:${selectedKind === 'preset' ? selectedRepoId : ''}:${selectedAutomationHydrated}`;
  }

  function syncDraftsForSelection(force: boolean): void {
    if (selectedKind === 'automation' && !selectedAutomationHydrated) return;
    const key = selectionKey();
    if (!force && key === syncedSelectionKey) return;
    syncedSelectionKey = key;
    const automation = selectedAutomation();
    if (automation) {
      detailName = automation.name;
      detailEnabled = automation.enabled;
      const scheduleFields = automation.product.scheduleEditor.fields;
      timezone = automation.product.scheduleEditor.timezone ?? automation.schedule?.timezone ?? timezone;
      detailHour = numberFromRecord(scheduleFields, 'hour', detailHour);
      detailMinute = numberFromRecord(scheduleFields, 'minute', detailMinute);
      detailWeekday = numberFromRecord(scheduleFields, 'weekday', detailWeekday);
      const executorPrompt = stringValue(automation.raw.executor, 'message_text') || stringValue(automation.raw.executor, 'message');
      promptDraft = automation.product.message.field === 'prompt' ? executorPrompt || automation.product.messagePreview : automation.product.messagePreview;
      ticketDraft = firstTicketBody(automation.raw.executor);
      detailExecutionMode = automation.executorKind;
      if (usesRuntimePicker(automation.executorKind)) {
        selectedAgent = stringValue(automation.raw.executor, 'agent') || defaultAgentId || agentIdFallback();
        selectedProfile = stringValue(automation.raw.executor, 'profile') || stringValue(automation.raw.executor, 'agent_profile');
        selectedModel = stringValue(automation.raw.executor, 'model');
        selectedReasoning = stringValue(automation.raw.executor, 'reasoning');
      } else {
        clearAgentModelSelection();
      }
      triggerDraft = prettyJson(automation.raw.trigger);
      filtersDraft = prettyJson(automation.raw.filters);
      targetDraft = prettyJson(automation.target);
      executorDraft = prettyJson(automation.raw.executor);
      policyDraft = prettyJson(automation.raw.policy);
      metadataDraft = prettyJson(automation.metadata);
      return;
    }
    const preset = selectedPreset();
    if (!preset) return;
    detailName = preset.name;
    detailEnabled = false;
    detailHour = preset.schedule.hour;
    detailMinute = preset.schedule.minute;
    detailWeekday = preset.schedule.weekday ?? 0;
    timezone = preset.schedule.timezone || timezone;
    detailExecutionMode = preset.executorKind;
    if (usesRuntimePicker(preset.executorKind)) {
      selectedAgent = defaultAgentId || agentIdFallback();
      selectedProfile = selectedAgent === 'hermes' ? defaultProfile : '';
      selectedModel = '';
      selectedReasoning = '';
    } else {
      clearAgentModelSelection();
    }
    promptDraft = renderPresetTemplate(preset.promptTemplate, selectedRepoId);
    ticketDraft = preset.ticketBodyTemplate ? renderPresetTemplate(preset.ticketBodyTemplate, selectedRepoId) : '';
    triggerDraft = prettyJson({ event_types: ['schedule.fire'] });
    filtersDraft = prettyJson({ schedule: { rule_id: '<assigned on save>' } });
    targetDraft = prettyJson(renderPresetValue(preset.targetShape, selectedRepoId));
    executorDraft = prettyJson(renderPresetValue(preset.executorShape, selectedRepoId, { prompt: promptDraft, ticket_body: ticketDraft }));
    policyDraft = prettyJson(preset.policy);
    metadataDraft = prettyJson({ preset: preset.id, automation_kind: preset.kind, repo_id: selectedRepoId });
  }

  function selectAutomation(automation: AutomationSummary): void {
    detailMode = 'detail';
    notice = null;
    if (routeRuleId !== automation.id) void goto(automationRoute(automation.id));
  }

  function selectPreset(preset: AutomationPresetDescriptor): void {
    selectedKind = 'preset';
    selectedId = preset.id;
    detailMode = 'detail';
    notice = null;
    if (routeRuleId) void goto(href('/automations'));
  }

  function replaceAutomation(updated: AutomationSummary): void {
    if (!overview) return;
    const nextAutomations = overview.automations.map((automation) => (automation.id === updated.id ? updated : automation));
    overview = {
      ...overview,
      automations: nextAutomations,
      summary: {
        total: nextAutomations.length,
        active: nextAutomations.filter((automation) => automation.enabled).length,
        paused: nextAutomations.filter((automation) => !automation.enabled).length,
        failedJobs: nextAutomations.filter((automation) => automation.lastJob?.effectiveState === 'failed').length
      }
    };
  }

  async function savePatch(patch: AutomationUpdateRequest, label: string): Promise<void> {
    const automation = selectedAutomation();
    if (!automation || saving) return;
    saving = true;
    error = null;
    const result = await webApi.hub.updateAutomation(automation.id, patch);
    saving = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    replaceAutomation(result.data);
    notice = `Saved ${label}`;
  }

  function schedulePatch(): AutomationUpdateRequest {
    return {
      timezone,
      hour: Number(detailHour),
      minute: Number(detailMinute),
      weekday: Number(detailWeekday)
    };
  }

  function saveTextDebounced(patch: AutomationUpdateRequest, label: string): void {
    const automation = selectedAutomation();
    if (selectedKind !== 'automation' || !automation) return;
    if ((label === 'name' && !automation.product.editable.canRename) || (label === 'prompt' && !automation.product.editable.canEditMessage)) return;
    if (saveTimer) window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      saveTimer = null;
      void savePatch(patch, label);
    }, 500);
  }

  async function saveJsonField(field: JsonField, draft: string): Promise<void> {
    const automation = selectedAutomation();
    if (automation && !automation.product.editable.canEditRaw && field !== 'metadata') {
      error = {
        kind: 'http',
        status: 400,
        code: 'AUTOMATION_PRODUCT_UNSUPPORTED_RAW_FIELDS',
        message: automation.product.editable.rawEditBlockedReason || 'Raw rule edits are available through the control-plane API.'
      };
      return;
    }
    const parsed = parseJsonDraft(draft);
    if (!parsed) return;
    await savePatch({ [field]: parsed } as AutomationUpdateRequest, field);
  }

  async function saveTicketPack(tickets: TicketPackTicket[]): Promise<void> {
    if (selectedKind !== 'automation') {
      ticketDraft = tickets[0]?.content ?? '';
      executorDraft = prettyJson({ ticket_pack: { source: 'inline', tickets } });
      return;
    }
    const automation = selectedAutomation();
    if (!automation) return;
    if (!automation.product.editable.canEditTicketBody) return;
    const executor = { ...asRecord(automation.raw.executor) };
    const ticketPack: JsonRecord = { ...asRecord(executor.ticket_pack), source: stringValue(executor.ticket_pack, 'source') || 'inline' };
    ticketPack.tickets = tickets.map((ticket) => ({ path: ticket.path, content: ticket.content }));
    executor.ticket_pack = ticketPack;
    executorDraft = prettyJson(executor);
    await savePatch({ ticket_body: tickets[0]?.content ?? '' }, 'ticket pack');
  }

  async function createPresetAutomation(): Promise<void> {
    if (!selectedRepoId || saving) return;
    const preset = selectedPreset();
    if (!preset) return;
    saving = true;
    error = null;
    const result = await webApi.hub.createAutomation({
      preset: preset.id,
      name: detailName,
      repo_id: selectedRepoId,
      timezone,
      hour: Number(detailHour),
      minute: Number(detailMinute),
      weekday: Number(detailWeekday),
      prompt: promptDraft || null,
      ticket_body: preset.executorKind === 'ticket_flow' ? ticketDraft || null : null,
      execution_mode: detailExecutionMode,
      agent: usesRuntimePicker(detailExecutionMode) ? selectedAgent || null : null,
      model: usesRuntimePicker(detailExecutionMode) ? selectedModel || null : null,
      reasoning: usesRuntimePicker(detailExecutionMode) ? selectedReasoning || null : null,
      profile: usesRuntimePicker(detailExecutionMode) ? selectedProfile || null : null,
      enabled: detailEnabled
    });
    saving = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    overview = overview
      ? { ...overview, automations: [...overview.automations, result.data] }
      : { automations: [result.data], presets, summary: { total: 1, active: result.data.enabled ? 1 : 0, paused: result.data.enabled ? 0 : 1, failedJobs: 0 } };
    notice = `Created ${result.data.name}${result.data.enabled ? '' : ' — paused'}`;
    detailMode = 'detail';
    await goto(automationRoute(result.data.id));
    await load();
  }

  async function runNow(automation: AutomationSummary): Promise<void> {
    actionId = automation.id;
    notice = null;
    const result = await webApi.hub.runAutomation(automation.id);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Queued ${automation.name}`;
    await load();
  }

  async function deleteAutomation(automation: AutomationSummary): Promise<void> {
    if (deleting) return;
    const confirmed = await confirmDialog({
      title: 'Delete automation',
      message: `Delete “${automation.name}”? This removes the rule and all of its schedules and run history. This can’t be undone.`,
      confirmText: 'Delete',
      cancelText: 'Cancel',
      danger: true
    });
    if (!confirmed) return;
    deleting = true;
    notice = null;
    const result = await webApi.hub.deleteAutomation(automation.id);
    deleting = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    if (overview) {
      const nextAutomations = overview.automations.filter((entry) => entry.id !== automation.id);
      overview = {
        ...overview,
        automations: nextAutomations,
        summary: {
          total: nextAutomations.length,
          active: nextAutomations.filter((entry) => entry.enabled).length,
          paused: nextAutomations.filter((entry) => !entry.enabled).length,
          failedJobs: nextAutomations.filter((entry) => entry.lastJob?.state === 'failed').length
        }
      };
    }
    selectedKind = null;
    selectedId = '';
    detailMode = 'list';
    notice = `Deleted ${automation.name}`;
    if (routeRuleId) await goto(href('/automations'));
  }

  async function setEnabled(automation: AutomationSummary, enabled: boolean): Promise<void> {
    actionId = automation.id;
    detailEnabled = enabled;
    const result = await webApi.hub.setAutomationEnabled(automation.id, enabled);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    replaceAutomation(result.data);
    notice = `${enabled ? 'Resumed' : 'Paused'} ${automation.name}`;
  }

  function createNewAutomationPrompt(): string {
    return [
      'I want to create a new CAR automation.',
      '',
      'Please help me define the trigger, schedule or event source, target repo/worktree, prompt or ticket flow, safety policy, and enabled state. Prefer a durable automation rule over a one-off command.'
    ].join('\n');
  }

  function editWithPmaPrompt(): string {
    const automation = selectedAutomation();
    if (automation) {
      return [
        `Please edit CAR automation ${automation.id}.`,
        '',
        'Read the existing automation rule and related run history from the hub automation data before making changes.',
        '',
        'Change request:',
        '<describe the change you want>'
      ].join('\n');
    }
    const preset = selectedPreset();
    if (!preset) return createNewAutomationPrompt();
    const agentLines =
      usesRuntimePicker(detailExecutionMode)
        ? [
            `Agent: ${selectedAgent || '(default)'}`,
            `Model: ${selectedModel || '(default)'}`,
            `Reasoning: ${selectedReasoning || '(default)'}`
          ]
        : ['Ticket-flow agent/model assignment should stay in ticket frontmatter.'];
    return [
      `I want to create an automation based on the ${preset.name} preset.`,
      '',
      `Repo: ${(selectedRepo?.label ?? selectedRepoId) || '(choose repo)'}`,
      ...agentLines,
      `Schedule: ${preset.schedule.kind} at ${pad(detailHour)}:${pad(detailMinute)} ${timezone}`,
      '',
      'Help me adapt this into the right automation rule.'
    ].join('\n');
  }

  function openPmaWithDraft(draft: string): void {
    void goto(href(`/chats?draft=${encodeURIComponent(draft)}`));
  }

  function formatDate(value: string | null): string {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  function scheduleLabel(automation: AutomationSummary): string {
    return automation.product.scheduleEditor.summary || (automation.schedule ? automation.schedule.scheduleKind : 'Event driven');
  }

  function targetLabel(automation: AutomationSummary | null): string {
    if (!automation) return selectedRepo?.label || selectedRepoId || 'Hub';
    const repoId = String(automation.product.targetSummary.repo_id ?? automation.product.targetSummary.base_repo_id ?? automation.target.repo_id ?? automation.target.base_repo_id ?? '');
    if (repoId) {
      const repo = targetOptions.find((entry) => entry.id === repoId);
      return repo?.label || repoId;
    }
    return String(automation.product.targetSummary.label ?? automation.target.worktree_id ?? 'Hub');
  }

  function selectedScheduleKind(): string {
    return selectedAutomation()?.product.scheduleEditor.kind ?? selectedPreset()?.schedule.kind ?? 'daily';
  }

  function selectedExecutorKind(): string {
    return detailExecutionMode || selectedAutomation()?.executorKind || selectedPreset()?.executorKind || '';
  }

  function selectedTargetPolicy(): string {
    return selectedAutomation()?.targetPolicy ?? selectedPreset()?.targetPolicy ?? '';
  }

  function detailDescription(): string {
    const automation = selectedAutomation();
    if (automation) return String(automation.metadata.description ?? automation.product.managed.reason ?? kindLabel(automation.kind));
    return selectedPreset()?.description ?? '';
  }

  function firstTicketBody(rawExecutor: unknown): string {
    const executor = asRecord(rawExecutor);
    const ticketPack = asRecord(executor.ticket_pack);
    const tickets = Array.isArray(ticketPack.tickets) ? ticketPack.tickets : [];
    return stringValue(asRecord(tickets[0]), 'content');
  }

  function selectedTicketPackTickets(): TicketPackTicket[] {
    const automation = selectedAutomation();
    if (automation) return ticketPackTickets(automation.raw.executor);
    if (selectedPreset()?.executorKind !== 'ticket_flow') return [];
    return ticketPackTickets({ ticket_pack: { tickets: [{ path: 'TICKET-001.md', content: ticketDraft }] } });
  }

  function ticketPackTickets(rawExecutor: unknown): TicketPackTicket[] {
    const executor = asRecord(rawExecutor);
    const ticketPack = asRecord(executor.ticket_pack);
    const tickets = Array.isArray(ticketPack.tickets) ? ticketPack.tickets : [];
    return tickets.map((ticket, index) => {
      const record = asRecord(ticket);
      return {
        path: stringValue(record, 'path') || `TICKET-${String(index + 1).padStart(3, '0')}.md`,
        content: stringValue(record, 'content')
      };
    });
  }

  function selectedRunHistory() {
    const automation = selectedAutomation();
    return automation ? runHistoryFromAutomationJobs(automation.jobs.map((job) => job.raw)) : [];
  }

  function agentIdFallback(): string {
    const first = agents[0];
    const raw = first?.id ?? first?.agent;
    return typeof raw === 'string' ? raw : '';
  }

  function clearAgentModelSelection(): void {
    selectedAgent = '';
    selectedProfile = '';
    selectedModel = '';
    selectedReasoning = '';
    models = [];
    loadingModels = false;
    modelCatalogAgentId = '';
    modelCatalogError = null;
  }

  async function loadModels(agentId: string, preferredModel = '', options: { keepReasoning?: boolean } = {}): Promise<void> {
    modelCatalogError = null;
    const initialSelection = resolveAgentModelSelection({ agents, agentId });
    if (!agentId || !initialSelection.canListModels) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    loadingModels = true;
    models = [];
    modelCatalogAgentId = agentId;
    const currentReasoning = selectedReasoning;
    const result = await webApi.pma.listAgentModels(agentId);
    loadingModels = false;
    if (!result.ok) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      modelCatalogError = result.error.message;
      return;
    }
    models = result.data;
    const selection = resolveAgentModelSelection({
      agents,
      agentId,
      catalog: result.data,
      preferredModel,
      currentModel: selectedModel,
      currentReasoning,
      keepReasoning: options.keepReasoning,
      allowEmptyModel: true
    });
    selectedModel = selection.model;
    selectedReasoning = selection.reasoning;
  }

  function handleAgentChange(): void {
    if (selectedAgent !== 'hermes') selectedProfile = '';
    selectedModel = '';
    selectedReasoning = '';
    void loadModels(selectedAgent);
  }

  function saveAgentModelFields(): void {
    const automation = selectedAutomation();
    if (!usesRuntimePicker(selectedExecutorKind()) || (automation && !automation.product.editable.canEditMessage)) return;
    void savePatch(
      {
        execution_mode: detailExecutionMode,
        agent: selectedAgent || null,
        model: selectedModel || null,
        reasoning: selectedReasoning || null,
        profile: selectedProfile || null
      },
      'agent settings'
    );
  }

  function onScheduleTime(event: Event): void {
    const value = (event.currentTarget as HTMLInputElement).value;
    const [h, m] = value.split(':');
    detailHour = Number(h) || 0;
    detailMinute = Number(m) || 0;
    const automation = selectedAutomation();
    if (automation?.product.editable.canEditSchedule) void savePatch(schedulePatch(), 'schedule');
  }

  function parseJsonDraft(draft: string): JsonRecord | null {
    try {
      const parsed = JSON.parse(draft || '{}');
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Expected a JSON object');
      return parsed as JsonRecord;
    } catch (err) {
      error = { kind: 'parse', status: null, code: 'invalid_json', message: err instanceof Error ? err.message : 'Invalid JSON' };
      return null;
    }
  }

  function asRecord(value: unknown): JsonRecord {
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
  }

  function prettyJson(value: unknown): string {
    return JSON.stringify(asRecord(value), null, 2);
  }

  function stringValue(value: unknown, key: string): string {
    const record = asRecord(value);
    const raw = record[key];
    return typeof raw === 'string' ? raw : '';
  }

  function numberFromRecord(value: unknown, key: string, fallback: number): number {
    const raw = asRecord(value)[key];
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function weekdayLabel(value: number, compact = false): string {
    const labels = compact ? ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] : ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    return labels[value] ?? labels[0];
  }

  function pad(value: number): string {
    return String(value).padStart(2, '0');
  }

  const KIND_LABELS: Record<string, string> = {
    security_scan_pr: 'Security scan',
    weekly_ticket_flow: 'Weekly ticket flow',
    pma_prompt: 'PMA reaction',
    agent_task_turn: 'Agent task',
    pma_operator_turn: 'PMA operator',
    ticket_flow: 'Ticket flow'
  };

  function kindLabel(kind: string): string {
    if (KIND_LABELS[kind]) return KIND_LABELS[kind];
    return kind.replace(/[_-]+/g, ' ').replace(/^./, (c) => c.toUpperCase());
  }

  function shortId(id: string): string {
    const tail = id.replace(/^[a-z]+[-:]/i, '');
    return (tail || id).slice(0, 6);
  }

  function automationStatus(automation: AutomationSummary): { key: string; dot: string; label: string } {
    if (automation.lastJob?.effectiveState === 'failed') return { key: 'failed', dot: 'status-failed', label: 'failed' };
    if (automation.enabled) return { key: 'active', dot: 'status-running', label: 'active' };
    return { key: 'paused', dot: 'status-idle', label: 'paused' };
  }

  function hasSchedule(): boolean {
    return selectedKind === 'preset' || selectedScheduleKind() !== 'event_driven';
  }

  function usesRuntimePicker(executorKind: string): boolean {
    return executorKind === 'agent_task_turn' || executorKind === 'pma_operator_turn';
  }

  function runtimeContractLabel(value: unknown, fallback = 'unspecified'): string {
    const contract = asRecord(value);
    const agent = String(contract.agent ?? '').trim();
    const model = String(contract.model ?? '').trim();
    if (agent && model) return `${agent} / ${model}`;
    return agent || model || fallback;
  }

  function agentRuntimeLabel(): string {
    const automation = selectedAutomation();
    if (automation) {
      const runtimeContract = asRecord(automation.raw.runtime_contract ?? automation.raw.runtimeContract);
      const contract = asRecord(automation.raw.direct_runtime_contract ?? automation.raw.directRuntimeContract ?? runtimeContract.requested);
      return runtimeContractLabel(contract, kindLabel(automation.executorKind));
    }
    if (usesRuntimePicker(selectedExecutorKind())) return selectedModel ? `${selectedAgent || 'default'} / ${selectedModel}` : selectedAgent || 'default agent';
    if (selectedExecutorKind() === 'ticket_flow') return 'Ticket flow';
    return kindLabel(selectedExecutorKind());
  }

  function coordinatorRuntimeLabel(): string {
    const automation = selectedAutomation();
    if (automation) {
      const contract = asRecord(automation.raw.coordinator_runtime_contract ?? automation.raw.coordinatorRuntimeContract);
      return runtimeContractLabel(contract, kindLabel(automation.executorKind));
    }
    return selectedModel ? `${selectedAgent || 'default'} / ${selectedModel}` : selectedAgent || 'default coordinator';
  }

  function workerRuntimeLabel(): string {
    const automation = selectedAutomation();
    if (!automation) return 'Chosen by PMA';
    const workers = automation.jobs
      .flatMap((job) => job.children)
      .filter((child) => String(child.child_kind ?? child.childKind ?? '') === 'agent_task')
      .map((child) => runtimeContractLabel(child.actual_runtime ?? child.actualRuntime ?? child.requested_runtime ?? child.requestedRuntime, 'unknown worker'));
    return [...new Set(workers)].join(', ') || 'Chosen by PMA';
  }

  function lastRunLabel(automation: AutomationSummary): string {
    const job = automation.lastJob;
    if (!job) return 'No runs yet';
    return job.effectiveState || job.state || 'unknown';
  }

  function lastRunDiagnostic(automation: AutomationSummary): string {
    const job = automation.lastJob;
    if (!job || !job.rawState || job.rawState === job.effectiveState) return '';
    return `Raw parent: ${job.rawState}`;
  }

  function lastRunBlocking(automation: AutomationSummary): string {
    const job = automation.lastJob;
    if (!job) return '';
    const reason = job.blockedReason || '';
    const blocker = job.blockedByJobId ? `blocked by ${job.blockedByJobId}` : '';
    return [reason, blocker].filter(Boolean).join(' · ');
  }

  function lastRunPolicyViolation(automation: AutomationSummary): string {
    const violation = automation.lastJob?.policyViolations?.[0];
    if (!violation) return '';
    return String(violation.message ?? violation.code ?? '');
  }

  function scheduleSummary(): string {
    const automation = selectedAutomation();
    if (automation) return automation.product.scheduleEditor.summary || scheduleLabel(automation);
    const preset = selectedPreset();
    if (!preset) return '';
    const time = `${pad(detailHour)}:${pad(detailMinute)}`;
    if (preset.schedule.kind === 'weekly') return `${weekdayLabel(detailWeekday, true)} ${time} ${timezone}`;
    return `Daily ${time} ${timezone}`;
  }

  function isManagedAutomation(automation: AutomationSummary): boolean {
    return automation.product.managed.managed || automation.systemOwned;
  }

  function canEditName(): boolean {
    const automation = selectedAutomation();
    return selectedKind === 'preset' || Boolean(automation?.product.editable.canRename);
  }

  function canEditSchedule(): boolean {
    const automation = selectedAutomation();
    return selectedKind === 'preset' || Boolean(automation?.product.editable.canEditSchedule);
  }

  function canEditPrompt(): boolean {
    const automation = selectedAutomation();
    return selectedKind === 'preset' || Boolean(automation?.product.editable.canEditMessage);
  }

  function canEditTicketBody(): boolean {
    const automation = selectedAutomation();
    return selectedKind === 'preset' || Boolean(automation?.product.editable.canEditTicketBody);
  }

  function canRunNow(): boolean {
    const automation = selectedAutomation();
    return Boolean(automation?.product.editable.canRunNow);
  }

  function canToggleEnabled(): boolean {
    const automation = selectedAutomation();
    if (!automation) return false;
    if (automation.enabled) return true;
    return Boolean(automation.product.editable.canEnable);
  }

  function canDelete(): boolean {
    const automation = selectedAutomation();
    if (!automation) return false;
    if (isManagedAutomation(automation)) return false;
    return !automation.enabled;
  }

  function selectedMessagePreview(): string {
    const automation = selectedAutomation();
    if (automation) return automation.product.messagePreview || automation.product.message.preview || 'No product-visible message source is declared.';
    return promptDraft || ticketDraft || selectedPreset()?.promptTemplate || '';
  }

  function scheduleFieldDateTime(): string {
    const automation = selectedAutomation();
    const dueAt = automation?.product.scheduleEditor.fields.due_at;
    return typeof dueAt === 'string' ? dueAt : '';
  }

  function scheduleFieldInterval(): string {
    const automation = selectedAutomation();
    const interval = automation?.product.scheduleEditor.fields.interval_seconds;
    return interval === undefined || interval === null ? '' : String(interval);
  }

  function rawLinkLabel(key: string): string {
    return key.replace(/_/g, ' ');
  }
</script>

{#snippet automationRow(automation: AutomationSummary)}
  {@const status = automationStatus(automation)}
  <li>
    <button
      type="button"
      class="automation-card status-{status.key}"
      class:selected={selectedKind === 'automation' && selectedId === automation.id}
      onclick={() => selectAutomation(automation)}
    >
      <span class="automation-avatar" style="--accent: {repoAccent(automation.name)}" aria-hidden="true">
        {repoInitials(automation.name)}
      </span>
      <span class="automation-card-body">
        <span class="automation-card-title">{automation.name}</span>
        <span class="automation-card-meta">
          <span class="status-dot {status.dot}"></span>
          <span>{status.label}</span>
          <span class="meta-dot">·</span>
          <span class="meta-schedule">{scheduleLabel(automation)}</span>
        </span>
      </span>
      <span class="card-chevron" aria-hidden="true">→</span>
    </button>
  </li>
{/snippet}

<svelte:head>
  <title>{selectedKind === 'automation' && detailName ? `${detailName} · Automations` : 'Automations'}</title>
</svelte:head>

<MasterDetail
  label="Automations workspace"
  selected={selectedKind !== null}
  mode={detailMode}
  listLabel="Automations"
  detailLabel="Detail"
  showSwitch={false}
  hideDetail={selectedKind === null}
  onModeChange={(mode) => {
    detailMode = mode;
    if (mode === 'list' && routeRuleId) void goto(href('/automations'));
  }}
>
  {#snippet list()}
    <aside class="automation-list" aria-label="Automations and presets">
      <header class="list-head">
        <div class="list-head-top">
          <div class="list-head-copy">
            <h1>Automations</h1>
            <p>Scheduled and event-driven PMA runs.</p>
          </div>
          <button
            type="button"
            class="icon-button"
            onclick={() => void load()}
            disabled={loading}
            aria-label="Refresh automations"
            title="Refresh"
          >
            {@render refreshIcon()}
          </button>
        </div>
        <button type="button" class="primary-button list-create" onclick={() => openPmaWithDraft(createNewAutomationPrompt())}>
          Create with PMA
        </button>
        {#if loading && overview}
          <span class="refresh-chip">Refreshing</span>
        {/if}
        {#if overview}
          <dl class="list-stats">
            <div class:active={overview.summary.active > 0}>
              <dd>{overview.summary.active}</dd>
              <dt>active</dt>
            </div>
            <div>
              <dd>{overview.summary.paused}</dd>
              <dt>paused</dt>
            </div>
            <div class:danger={overview.summary.failedJobs > 0}>
              <dd>{overview.summary.failedJobs}</dd>
              <dt>failed</dt>
            </div>
          </dl>
        {/if}
      </header>

      <div class="list-scroll">
        {#if loading && !overview}
          <ContentSkeleton variant="chat-list" rows={4} />
        {:else}
          <div class="list-group">
            <div class="list-group-head">
              <h2>Your automations</h2>
              <span>{userAutomations.length}</span>
            </div>
            {#if userAutomations.length === 0}
              <p class="group-empty">Nothing yet. Create one with PMA, or start from a preset below.</p>
            {:else}
              <ul class="card-list">
                {#each userAutomations as automation (automation.id)}
                  {@render automationRow(automation)}
                {/each}
              </ul>
            {/if}
          </div>

          <div class="list-group">
            <div class="list-group-head">
              <h2>Start from a preset</h2>
            </div>
            <ul class="card-list">
              {#each presets as preset}
                <li>
                  <button
                    type="button"
                    class="automation-card preset-card"
                    class:selected={selectedKind === 'preset' && selectedId === preset.id}
                    onclick={() => selectPreset(preset)}
                  >
                    <span class="automation-avatar preset-avatar" aria-hidden="true">+</span>
                    <span class="automation-card-body">
                      <span class="automation-card-title">{preset.name}</span>
                      <span class="automation-card-meta">
                        <span>{preset.schedule.kind}</span>
                      </span>
                    </span>
                    <span class="card-chevron" aria-hidden="true">→</span>
                  </button>
                </li>
              {/each}
            </ul>
          </div>

          {#if managedAutomations.length > 0}
            <details class="list-group system-group">
              <summary>
                <span class="system-summary-label">Managed diagnostics</span>
                <span class="system-count">{managedAutomations.length}</span>
              </summary>
              <p class="group-empty system-note">Built-in, PMA-mirrored, and legacy-migrated rules. They are diagnostics-first and only typed editable fields can be changed.</p>
              <ul class="card-list">
                {#each managedAutomations as automation (automation.id)}
                  {@render automationRow(automation)}
                {/each}
              </ul>
            </details>
          {/if}
        {/if}
      </div>
    </aside>
  {/snippet}

  {#snippet detail()}
    {#if loading && !overview}
      <section class="automation-detail automation-detail-empty" aria-label="Automation detail">
        <ContentSkeleton variant="detail" rows={4} />
      </section>
    {:else if routeAutomationMissing}
      <section class="automation-detail automation-detail-empty" aria-label="Automation detail">
        <div class="detail-empty">
          <p>Automation not found.</p>
          <a class="ghost-button" href={href('/automations')}>Back to automations</a>
        </div>
      </section>
    {:else if selectedKind === 'automation' && !selectedAutomationHydrated}
      <section class="automation-detail automation-detail-empty" aria-label="Automation detail">
        <ContentSkeleton variant="detail" rows={4} />
      </section>
    {:else if selectedKind === null || !selectionResolved}
      <section class="automation-detail automation-detail-empty" aria-label="Automation detail">
        <div class="detail-empty">
          <p>Pick an automation from the list, or start from a preset.</p>
        </div>
      </section>
    {:else}
    <section class="automation-detail" aria-label="Automation detail">
      {#if error}
        <div class="automation-notice error" role="alert">{error.message}</div>
      {/if}
      {#if detailError}
        <div class="automation-notice error" role="alert">{detailError.message}</div>
      {/if}
      {#if notice}
        <div class="automation-notice success" role="status">{notice}</div>
      {/if}

      <header class="detail-head">
        <div class="detail-head-copy">
          <h2>{detailName || 'Automation'}</h2>
          <p class="detail-desc">{detailDescription()}</p>
        </div>
        <div class="detail-actions">
          {#if selectedAutomation()}
            <button
              type="button"
              class="ghost-button"
              disabled={!canRunNow() || actionId === selectedAutomation()?.id}
              onclick={() => selectedAutomation() && runNow(selectedAutomation() as AutomationSummary)}
            >Run now</button>
            <button
              type="button"
              class="ghost-button"
              disabled={!canToggleEnabled() || actionId === selectedAutomation()?.id}
              onclick={() => selectedAutomation() && setEnabled(selectedAutomation() as AutomationSummary, !detailEnabled)}
            >{detailEnabled ? 'Pause' : 'Resume'}</button>
            <button type="button" class="ghost-button" onclick={() => openPmaWithDraft(editWithPmaPrompt())}>Edit with PMA</button>
            {#if canDelete()}
              <button
                type="button"
                class="ghost-button danger"
                disabled={deleting || actionId === selectedAutomation()?.id}
                onclick={() => selectedAutomation() && void deleteAutomation(selectedAutomation() as AutomationSummary)}
              >{deleting ? 'Deleting…' : 'Delete'}</button>
            {/if}
          {:else}
            <button type="button" class="ghost-button" onclick={() => openPmaWithDraft(editWithPmaPrompt())}>Adapt with PMA</button>
            <button
              type="button"
              class="primary-button"
              disabled={!selectedRepoId || saving}
              onclick={() => void createPresetAutomation()}
            >{saving ? 'Creating…' : 'Create automation'}</button>
          {/if}
        </div>
      </header>

      {#if selectedKind === 'preset'}
        <p class="detail-banner">This is a template. Adjust the settings below, then create it to start running.</p>
      {:else if selectedAutomation()?.product.managed.managed}
        <p class="detail-banner system">
          {selectedAutomation()?.product.managed.reason || 'Managed automation.'}
        </p>
      {/if}

      <!-- At a glance: read-only runtime facts -->
      <dl class="fact-grid">
        <div class="fact">
          <dt>Schedule</dt>
          <dd>{scheduleSummary()}</dd>
        </div>
        {#if selectedExecutorKind() === 'pma_operator_turn'}
          <div class="fact">
            <dt>Coordinator</dt>
            <dd>{coordinatorRuntimeLabel()}</dd>
          </div>
          <div class="fact">
            <dt>Workers</dt>
            <dd>{workerRuntimeLabel()}</dd>
          </div>
        {:else}
          <div class="fact">
            <dt>Agent</dt>
            <dd>{agentRuntimeLabel()}</dd>
          </div>
        {/if}
        <div class="fact">
          <dt>Target</dt>
          <dd>{targetLabel(selectedAutomation())}</dd>
        </div>
        {#if selectedAutomation()}
          <div class="fact">
            <dt>Next run</dt>
            <dd>{formatDate(selectedAutomation()?.schedule?.nextFireAt ?? null)}</dd>
          </div>
          <div class="fact">
            <dt>Last run</dt>
            <dd class="fact-status fact-{selectedAutomation()?.lastJob?.effectiveState ?? 'none'}">
              {lastRunLabel(selectedAutomation() as AutomationSummary)}
              {#if lastRunDiagnostic(selectedAutomation() as AutomationSummary)}
                <span class="raw-state-diagnostic">{lastRunDiagnostic(selectedAutomation() as AutomationSummary)}</span>
              {/if}
              {#if lastRunBlocking(selectedAutomation() as AutomationSummary)}
                <span class="blocked-state-diagnostic">{lastRunBlocking(selectedAutomation() as AutomationSummary)}</span>
              {/if}
              {#if lastRunPolicyViolation(selectedAutomation() as AutomationSummary)}
                <span class="policy-state-diagnostic">{lastRunPolicyViolation(selectedAutomation() as AutomationSummary)}</span>
              {/if}
            </dd>
          </div>
        {/if}
      </dl>

      <!-- Settings: structured, human-editable fields -->
      <div class="detail-section">
        <h3>Settings</h3>
        <div class="field-grid">
          <label class="field">
            <span>Name</span>
            <input bind:value={detailName} disabled={!canEditName()} oninput={() => saveTextDebounced({ name: detailName }, 'name')} />
          </label>

          {#if selectedKind === 'preset'}
            <label class="field">
              <span>Repo</span>
              <select bind:value={selectedRepoId}>
                {#each presetTargetOptions as repo}
                  <option value={repo.id} disabled={repo.disabled}>{repo.label || repo.id}</option>
                {/each}
              </select>
            </label>
            {#if selectedPreset()?.id === 'security_scan_pr'}
              <label class="field">
                <span>Execution</span>
                <select bind:value={detailExecutionMode}>
                  <option value="agent_task_turn">Run with agent</option>
                  <option value="pma_operator_turn">Run with PMA</option>
                </select>
              </label>
            {/if}
            <label class="field">
              <span>Start</span>
              <select bind:value={detailEnabled}>
                <option value={false}>Paused</option>
                <option value={true}>Active</option>
              </select>
            </label>
          {:else if selectedAutomation()?.kind === 'security_scan_pr' && canEditPrompt()}
            <label class="field">
              <span>Execution</span>
              <select bind:value={detailExecutionMode} onchange={saveAgentModelFields}>
                <option value="agent_task_turn">Run with agent</option>
                <option value="pma_operator_turn">Run with PMA</option>
              </select>
            </label>
          {/if}

          {#if hasSchedule() && ['daily', 'weekly'].includes(selectedScheduleKind())}
            <label class="field">
              <span>Time</span>
              <input type="time" value={scheduleTimeValue} disabled={!canEditSchedule()} onchange={onScheduleTime} />
            </label>
            <label class="field">
              <span>Timezone</span>
              <input bind:value={timezone} disabled={!canEditSchedule()} onchange={() => selectedAutomation()?.product.editable.canEditSchedule && void savePatch(schedulePatch(), 'schedule')} />
            </label>
            {#if selectedScheduleKind() === 'weekly'}
              <label class="field">
                <span>Day</span>
                <select bind:value={detailWeekday} disabled={!canEditSchedule()} onchange={() => selectedAutomation()?.product.editable.canEditSchedule && void savePatch(schedulePatch(), 'schedule')}>
                  {#each [0, 1, 2, 3, 4, 5, 6] as day}
                    <option value={day}>{weekdayLabel(day)}</option>
                  {/each}
                </select>
              </label>
            {/if}
          {:else if selectedKind === 'automation' && selectedScheduleKind() === 'one_shot'}
            <label class="field">
              <span>Due at</span>
              <input value={scheduleFieldDateTime()} disabled />
            </label>
          {:else if selectedKind === 'automation' && selectedScheduleKind() === 'interval'}
            <label class="field">
              <span>Interval seconds</span>
              <input value={scheduleFieldInterval()} disabled />
            </label>
          {:else if selectedKind === 'automation'}
            <div class="field readonly-field">
              <span>Schedule</span>
              <strong>{scheduleSummary()}</strong>
            </div>
          {/if}
        </div>

        {#if usesRuntimePicker(selectedExecutorKind()) && canEditPrompt()}
          <div class="agent-picker-row">
            {#if loadingAgents}
              <p class="detail-hint">Loading agent controls…</p>
            {:else if agentCatalogError}
              <p class="detail-hint error-text">{agentCatalogError}</p>
            {/if}
            <AgentModelReasoningPicker
              {agents}
              fallbackAgentIds={defaultAgentId ? [defaultAgentId] : []}
              bind:agentValue={selectedAgent}
              bind:profileValue={selectedProfile}
              bind:modelValue={selectedModel}
              bind:reasoningValue={selectedReasoning}
              {models}
              loading={loadingAgents || loadingModels}
              modelCatalogError={agentCatalogError ?? modelCatalogError}
              variant="ticket"
              allowEmptyModelOption={true}
              unsetModelLabel="Default model"
              emptyModelLabel="Configured model"
              defaultReasoningLabel="default"
              onAgentChange={handleAgentChange}
              onchange={saveAgentModelFields}
            />
          </div>
        {:else if selectedExecutorKind() === 'ticket_flow'}
          <p class="detail-hint">Agent and model for ticket flows are set per-ticket in the ticket frontmatter.</p>
        {/if}
      </div>

      <!-- Instruction: prompt or ticket body -->
      <div class="detail-section">
        {#if selectedExecutorKind() === 'ticket_flow'}
          <h3>Ticket pack</h3>
          <TicketPackEditor
            tickets={selectedTicketPackTickets()}
            {agents}
            onChange={canEditTicketBody() ? saveTicketPack : undefined}
            allowAddRemove={selectedKind === 'automation' && canEditTicketBody()}
          />
        {:else}
          <div class="section-head-row">
            <h3>Message</h3>
            {#if selectedAutomation()}
              <span class="source-chip">{selectedAutomation()?.product.messageSource || 'none'}</span>
            {/if}
          </div>
          {#if canEditPrompt()}
            <textarea
              class="instruction-editor"
              bind:value={promptDraft}
              oninput={() => saveTextDebounced({ prompt: promptDraft }, 'prompt')}
            ></textarea>
          {:else}
            <div class="message-preview">
              <p>{selectedMessagePreview()}</p>
            </div>
          {/if}
        {/if}
      </div>

      {#if selectedAutomation() && selectedAutomation()?.product.diagnostics.length}
        <div class="detail-section diagnostics-section">
          <h3>Diagnostics</h3>
          <ul class="diagnostic-list">
            {#each selectedAutomation()?.product.diagnostics ?? [] as diagnostic}
              <li>
                <strong>{String(diagnostic.code ?? 'AUTOMATION_DIAGNOSTIC')}</strong>
                <span>{String(diagnostic.message ?? '')}</span>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      {#if selectedAutomation()}
        <div class="detail-section">
          <h3>Run history</h3>
          <RunHistoryList runs={selectedRunHistory()} emptyMessage="This automation has not run yet." />
        </div>
      {/if}

      <!-- Advanced: raw rule config, diagnostic/admin inspection only. -->
      <details class="advanced">
        <summary>
          <span>Diagnostic raw inspection</span>
          <span class="advanced-hint">{selectedAutomation()?.product.editable.rawEditBlockedReason || 'Raw rule edits use the control-plane API.'}</span>
        </summary>
        {#if selectedAutomation()}
          <div class="raw-link-row">
            {#each Object.entries(selectedAutomation()?.product.rawLinks ?? {}) as [key, value]}
              {#if typeof value === 'string'}
                <a class="ghost-button" href={href(value)}>{rawLinkLabel(key)}</a>
              {/if}
            {/each}
          </div>
        {/if}
        <div class="json-grid">
          <label class="field">
            <span>Trigger</span>
            <textarea class="json-editor" bind:value={triggerDraft} readonly></textarea>
          </label>
          <label class="field">
            <span>Filters</span>
            <textarea class="json-editor" bind:value={filtersDraft} readonly></textarea>
          </label>
          <label class="field">
            <span>Target</span>
            <textarea class="json-editor" bind:value={targetDraft} readonly></textarea>
          </label>
          <label class="field">
            <span>Executor</span>
            <textarea class="json-editor" bind:value={executorDraft} readonly></textarea>
          </label>
          <label class="field">
            <span>Policy</span>
            <textarea class="json-editor" bind:value={policyDraft} readonly></textarea>
          </label>
          <label class="field">
            <span>Metadata</span>
            <textarea class="json-editor" bind:value={metadataDraft} onblur={() => void saveJsonField('metadata', metadataDraft)}></textarea>
          </label>
        </div>
      </details>
    </section>
    {/if}
  {/snippet}
</MasterDetail>

{#snippet refreshIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 12a9 9 0 0 1-15.36 6.36" />
    <path d="M3 12a9 9 0 0 1 15.36-6.36" />
    <path d="M21 4v5h-5" />
    <path d="M3 20v-5h5" />
  </svg>
{/snippet}

<style>
  .automation-notice {
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-1);
  }

  .automation-notice.error {
    background: var(--color-danger-soft);
    border-color: color-mix(in srgb, var(--color-danger) 30%, transparent);
    color: var(--color-danger);
  }

  .automation-notice.success {
    background: var(--color-success-soft);
    border-color: color-mix(in srgb, var(--color-success) 30%, transparent);
    color: var(--color-success);
  }

  /* ---- List column ---- */
  .automation-list {
    display: flex;
    flex-direction: column;
    min-height: 0;
    gap: var(--space-3);
  }

  .list-head {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    flex: 0 0 auto;
  }

  .list-head-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .list-head-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .list-head-copy h1 {
    margin: 0;
    font-size: var(--font-size-3);
    font-weight: 680;
    letter-spacing: -0.02em;
    color: var(--color-ink);
  }

  .list-head-copy p {
    margin: 0;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
  }

  .icon-button {
    flex: 0 0 auto;
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
    padding: 0;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: var(--font-size-2);
    cursor: pointer;
    transition: border-color var(--transition-fast), color var(--transition-fast);
  }

  .icon-button:hover:not(:disabled) {
    border-color: var(--color-border-strong);
    color: var(--color-ink);
  }

  .icon-button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .list-create {
    width: 100%;
  }

  .refresh-chip {
    align-self: flex-start;
    padding: 2px var(--space-2);
    border-radius: var(--radius-2);
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    font-size: 10px;
    font-weight: 650;
    text-transform: uppercase;
  }

  .list-stats {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    background: var(--color-border-subtle);
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    overflow: hidden;
  }

  .list-stats > div {
    display: grid;
    gap: 1px;
    padding: var(--space-2);
    background: var(--color-surface-sunken);
  }

  .list-stats dd {
    margin: 0;
    font-size: var(--font-size-2);
    font-weight: 680;
    color: var(--color-ink-muted);
    font-variant-numeric: tabular-nums;
  }

  .list-stats dt {
    margin: 0;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--color-ink-faint);
  }

  .list-stats > div.active dd { color: var(--color-success); }
  .list-stats > div.danger dd { color: var(--color-danger); }

  .list-scroll {
    flex: 1 1 0%;
    min-height: 0;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding-right: 2px;
  }

  .list-group {
    display: grid;
    gap: var(--space-2);
    align-content: start;
  }

  .group-empty {
    margin: 0;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    background: var(--color-surface-sunken);
    border-radius: var(--radius-2);
    padding: var(--space-3);
  }

  .list-group-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-2);
    padding: 0 var(--space-1);
  }

  .list-group-head h2 {
    margin: 0;
    font-size: var(--font-size-1);
    font-weight: 650;
    letter-spacing: -0.01em;
    color: var(--color-ink);
  }

  .list-group-head span {
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    font-variant-numeric: tabular-nums;
  }

  /* ---- System group disclosure ---- */
  .system-group {
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface-sunken);
    padding: var(--space-2);
  }

  .system-group > summary {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    list-style: none;
    padding: var(--space-1) var(--space-2);
    font-size: var(--font-size-1);
    font-weight: 650;
    color: var(--color-ink-soft);
  }

  .system-group > summary::-webkit-details-marker {
    display: none;
  }

  .system-group > summary::before {
    content: '▸';
    color: var(--color-ink-faint);
    font-size: 10px;
    transition: transform var(--transition-fast);
  }

  .system-group[open] > summary::before {
    transform: rotate(90deg);
  }

  .system-summary-label {
    flex: 1 1 auto;
  }

  .system-count {
    flex: 0 0 auto;
    font-size: var(--font-size-0);
    font-weight: 600;
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    border-radius: 999px;
    padding: 1px 8px;
    font-variant-numeric: tabular-nums;
  }

  .system-note {
    margin: var(--space-1) 0 var(--space-2);
    background: transparent;
    padding: 0 var(--space-2);
  }

  .system-group .card-list {
    margin-top: var(--space-1);
  }

  .card-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 6px;
  }

  .automation-card {
    position: relative;
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
    text-align: left;
    cursor: pointer;
    overflow: hidden;
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .automation-card::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: transparent;
  }

  .automation-card.status-active::before { background: var(--color-success); }
  .automation-card.status-failed::before { background: var(--color-danger); }

  .automation-card:hover {
    border-color: var(--color-border-strong);
    box-shadow: var(--shadow-card-hover);
  }

  .automation-card.selected {
    border-color: var(--color-accent);
    box-shadow: inset 0 0 0 1px var(--color-accent);
  }

  .automation-avatar {
    flex: 0 0 auto;
    width: 28px;
    height: 28px;
    border-radius: 7px;
    display: grid;
    place-items: center;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--accent, var(--color-accent));
    background: color-mix(in srgb, var(--accent, var(--color-accent)) 12%, white);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent, var(--color-accent)) 18%, transparent);
  }

  .preset-avatar {
    --accent: var(--color-ink-muted);
    font-size: var(--font-size-2);
    font-weight: 500;
  }

  .automation-card-body {
    flex: 1 1 auto;
    min-width: 0;
    display: grid;
    gap: 1px;
  }

  .automation-card-title {
    font-size: var(--font-size-1);
    font-weight: 600;
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    line-height: 1.25;
  }

  .automation-card-meta {
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    gap: 5px;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    overflow: hidden;
    min-width: 0;
  }

  .automation-card-meta > .meta-schedule {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta-dot {
    color: var(--color-ink-faint);
    opacity: 0.7;
  }

  .card-chevron {
    flex: 0 0 auto;
    color: var(--color-ink-faint);
    opacity: 0;
    transform: translateX(-4px);
    transition: opacity var(--transition-base), transform var(--transition-base);
  }

  .automation-card:hover .card-chevron,
  .automation-card.selected .card-chevron {
    opacity: 1;
    transform: translateX(0);
  }

  /* ---- Detail column ---- */
  .automation-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
    padding: var(--space-5);
    min-width: 0;
    min-height: 0;
    overflow: auto;
  }

  /* Flex children default to shrinking; that squashes the fact grid and other
     fixed-content blocks. Pin them to natural height and let the pane scroll. */
  .automation-detail > * {
    flex: 0 0 auto;
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
  }

  .detail-head-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .detail-head h2 {
    margin: 0;
    font-size: var(--font-size-3);
    font-weight: 650;
    letter-spacing: -0.018em;
    color: var(--color-ink);
  }

  .detail-desc {
    margin: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink-muted);
    max-width: 60ch;
  }

  .detail-actions {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .automation-detail-empty {
    display: grid;
    place-items: center;
    min-height: 60vh;
  }

  .detail-empty {
    display: grid;
    justify-items: center;
    gap: var(--space-3);
    text-align: center;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  .detail-banner {
    margin: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink-soft);
    background: var(--color-surface-sunken);
    border-radius: var(--radius-2);
    padding: var(--space-2) var(--space-3);
  }

  .detail-banner.system {
    background: color-mix(in srgb, var(--color-accent) 8%, var(--color-surface-sunken));
    border: 1px solid color-mix(in srgb, var(--color-accent) 22%, transparent);
  }

  /* ---- At-a-glance facts ---- */
  .fact-grid {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 1px;
    background: var(--color-border-subtle);
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    overflow: hidden;
  }

  .fact {
    display: grid;
    gap: 2px;
    padding: var(--space-3);
    background: var(--color-surface-sunken);
  }

  .fact dt {
    margin: 0;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--color-ink-faint);
  }

  .fact dd {
    margin: 0;
    font-size: var(--font-size-1);
    font-weight: 550;
    color: var(--color-ink);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .fact-status {
    text-transform: capitalize;
  }

  .raw-state-diagnostic,
  .blocked-state-diagnostic,
  .policy-state-diagnostic {
    display: block;
    margin-top: 2px;
    font-size: 11px;
    font-weight: 500;
    text-transform: none;
  }

  .raw-state-diagnostic {
    color: var(--color-warning);
  }

  .blocked-state-diagnostic,
  .policy-state-diagnostic {
    color: var(--color-danger);
  }

  .fact-failed { color: var(--color-danger); }
  .fact-running { color: var(--color-success); }
  .fact-none { color: var(--color-ink-muted); }

  /* ---- Sections ---- */
  .detail-section {
    display: grid;
    gap: var(--space-3);
    padding-top: var(--space-4);
    border-top: 1px solid var(--color-border-subtle);
  }

  .detail-section h3 {
    margin: 0;
    font-size: var(--font-size-1);
    font-weight: 650;
    color: var(--color-ink);
  }

  .field-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: var(--space-3);
  }

  .field {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .field > span {
    font-size: var(--font-size-0);
    font-weight: 500;
    color: var(--color-ink-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .agent-picker-row {
    display: grid;
    gap: var(--space-3);
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    position: relative;
    z-index: 5;
  }

  .detail-hint {
    margin: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink-muted);
    background: var(--color-surface-sunken);
    border-radius: var(--radius-2);
    padding: var(--space-2) var(--space-3);
  }

  input,
  select,
  textarea {
    width: 100%;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    color: var(--color-ink);
    font-size: var(--font-size-1);
    min-height: 34px;
    padding: 6px 10px;
  }

  input:focus-visible,
  select:focus-visible,
  textarea:focus-visible {
    outline: none;
    border-color: var(--color-accent);
    box-shadow: var(--shadow-focus);
  }

  input:disabled,
  select:disabled,
  textarea:read-only {
    color: var(--color-ink-muted);
    background: var(--color-surface-sunken);
    cursor: default;
  }

  .readonly-field {
    min-height: 58px;
    padding: 6px 10px;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
  }

  .readonly-field strong {
    font-size: var(--font-size-1);
    font-weight: 550;
    color: var(--color-ink);
  }

  .section-head-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .source-chip {
    max-width: 100%;
    border-radius: 999px;
    padding: 2px 8px;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-family: var(--font-mono);
    font-size: 11px;
    overflow-wrap: anywhere;
  }

  .instruction-editor {
    min-height: 150px;
    line-height: 1.5;
    resize: vertical;
  }

  .message-preview {
    min-height: 86px;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    padding: var(--space-3);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .message-preview p {
    margin: 0;
    line-height: 1.5;
  }

  .diagnostic-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-2);
  }

  .diagnostic-list li {
    display: grid;
    gap: 3px;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface-sunken);
  }

  .diagnostic-list strong {
    color: var(--color-ink);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .diagnostic-list span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  /* ---- Advanced disclosure ---- */
  .advanced {
    border-top: 1px solid var(--color-border-subtle);
    padding-top: var(--space-3);
  }

  .advanced > summary {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    flex-wrap: wrap;
    cursor: pointer;
    list-style: none;
    font-size: var(--font-size-1);
    font-weight: 650;
    color: var(--color-ink-soft);
  }

  .advanced > summary::-webkit-details-marker {
    display: none;
  }

  .advanced > summary::before {
    content: '▸';
    color: var(--color-ink-faint);
    font-size: 10px;
    transition: transform var(--transition-fast);
  }

  .advanced[open] > summary::before {
    transform: rotate(90deg);
  }

  .advanced-hint {
    font-size: var(--font-size-0);
    font-weight: 500;
    color: var(--color-ink-faint);
  }

  .json-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: var(--space-3);
    margin-top: var(--space-3);
  }

  .raw-link-row {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-top: var(--space-3);
  }

  .raw-link-row :global(.ghost-button) {
    text-transform: capitalize;
    text-decoration: none;
  }

  .json-editor {
    font-family: var(--font-mono);
    font-size: var(--font-size-0);
    min-height: 150px;
    line-height: 1.45;
    resize: vertical;
    background: var(--color-surface-sunken);
  }

  @media (max-width: 1020px) {
    .automation-detail {
      padding: var(--space-4);
    }

    .detail-actions {
      width: 100%;
    }
  }
</style>
