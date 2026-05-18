<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import RunHistoryList from '$lib/components/tickets/RunHistoryList.svelte';
  import TicketPackEditor from '$lib/components/tickets/TicketPackEditor.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import { runHistoryFromAutomationJobs } from '$lib/viewModels/runHistory';
  import {
    webApi,
    type ApiError,
    type AutomationOverview,
    type AutomationSummary,
    type AutomationUpdateRequest,
    type JsonRecord
  } from '$lib/api/client';
  import type { RepoSummary } from '$lib/viewModels/domain';
  import { resolveAgentModelSelection } from '$lib/viewModels/modelPickers';

  type PresetId = 'security_scan_pr' | 'weekly_ticket_flow';
  type SelectionKind = 'automation' | 'preset';
  type JsonField = 'trigger' | 'filters' | 'target' | 'executor' | 'policy' | 'metadata';
  type TicketPackTicket = { path: string; content: string };

  type AutomationPreset = {
    id: PresetId;
    name: string;
    kind: string;
    description: string;
    scheduleKind: 'daily' | 'weekly';
    executorKind: string;
    targetPolicy: string;
    defaultHour: number;
    defaultWeekday?: number;
    prompt: string;
    ticketBody?: string;
  };

  const PRESETS: AutomationPreset[] = [
    {
      id: 'security_scan_pr',
      name: 'Daily Security Scan',
      kind: 'security_scan_pr',
      description: 'PMA scans a repo with existing security tooling and opens a focused draft PR when it finds actionable issues.',
      scheduleKind: 'daily',
      executorKind: 'pma_turn',
      targetPolicy: 'hub',
      defaultHour: 9,
      prompt:
        "Run a security scan for the selected repo. Inspect dependency, secret, and static-analysis findings using the repo's existing tooling. If actionable issues are discovered, create a focused fix branch, make the smallest safe changes, run relevant checks, and open a draft PR with findings and verification. If no issues are found, summarize the clean result."
    },
    {
      id: 'weekly_ticket_flow',
      name: 'Weekly Preset Ticket Flow',
      kind: 'weekly_ticket_flow',
      description: "A scheduled ticket flow runs in a fresh automation worktree from the selected repo's remote default branch.",
      scheduleKind: 'weekly',
      executorKind: 'ticket_flow',
      targetPolicy: 'new_automation_worktree',
      defaultHour: 10,
      defaultWeekday: 0,
      prompt: 'Run the configured weekly maintenance ticket flow and open a draft PR for useful changes.',
      ticketBody:
        'You are running a scheduled weekly ticket flow.\n\n- Sync context from `.codex-autorunner/contextspace/` and inspect the current repo state.\n- Run dependency, test, lint, and maintenance checks that are already standard for this repo.\n- Fix small, well-bounded issues that are clearly safe.\n- If changes are made, run relevant verification and open a draft PR with a concise summary.\n- If no changes are needed, record the checks performed and mark this ticket done.\n'
    }
  ];

  let overview = $state<AutomationOverview | null>(null);
  let repos = $state<RepoSummary[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let loading = $state(true);
  let loadingModels = $state(false);
  let saving = $state(false);
  let actionId = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let notice = $state<string | null>(null);
  let selectedKind = $state<SelectionKind>('preset');
  let selectedId = $state<string>(PRESETS[0].id);
  let saveTimer: number | null = null;

  let selectedRepoId = $state('');
  let defaultAgentId = $state('');
  let defaultProfile = $state('');
  let selectedAgent = $state('');
  let selectedProfile = $state('');
  let selectedModel = $state('');
  let selectedReasoning = $state('');
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

  const automations = $derived(overview?.automations ?? []);
  const selectedRepo = $derived(repos.find((repo) => repo.id === selectedRepoId) ?? null);
  const scheduleTimeValue = $derived(`${pad(detailHour)}:${pad(detailMinute)}`);

  onMount(() => {
    void load();
    return () => {
      if (saveTimer) window.clearTimeout(saveTimer);
    };
  });

  async function load(): Promise<void> {
    loading = true;
    error = null;
    const [automationResult, repoResult, agentResult] = await Promise.all([
      webApi.hub.listAutomations(),
      webApi.hub.listRepos(),
      webApi.pma.listAgents()
    ]);
    if (automationResult.ok) overview = automationResult.data;
    else error = automationResult.error;
    if (repoResult.ok) {
      repos = repoResult.data;
      if (!selectedRepoId && repos[0]) selectedRepoId = repos[0].id;
    }
    if (agentResult.ok) {
      agents = agentResult.data.agents;
      defaultAgentId = agentResult.data.default;
      defaultProfile = String(agentResult.data.defaults.profile ?? '');
    }
    if (automationResult.ok && automationResult.data.automations.length > 0) {
      selectedKind = 'automation';
      selectedId = automationResult.data.automations[0].id;
    }
    loading = false;
    syncDraftsForSelection(true);
  }

  $effect(() => {
    syncDraftsForSelection(false);
  });

  function selectedAutomation(): AutomationSummary | null {
    return selectedKind === 'automation' ? automations.find((automation) => automation.id === selectedId) ?? null : null;
  }

  function selectedPreset(): AutomationPreset {
    return PRESETS.find((preset) => preset.id === selectedId) ?? PRESETS[0];
  }

  function selectionKey(): string {
    return `${selectedKind}:${selectedId}:${selectedAutomation()?.updatedAt ?? ''}:${selectedKind === 'preset' ? selectedRepoId : ''}`;
  }

  function syncDraftsForSelection(force: boolean): void {
    const key = selectionKey();
    if (!force && key === syncedSelectionKey) return;
    syncedSelectionKey = key;
    const automation = selectedAutomation();
    if (automation) {
      detailName = automation.name;
      detailEnabled = automation.enabled;
      timezone = automation.schedule?.timezone ?? timezone;
      detailHour = numberFromRecord(automation.schedule?.schedule, 'hour', detailHour);
      detailMinute = numberFromRecord(automation.schedule?.schedule, 'minute', detailMinute);
      const weekdays = automation.schedule?.schedule.weekdays;
      detailWeekday = Array.isArray(weekdays) ? Number(weekdays[0] ?? 0) : detailWeekday;
      promptDraft = stringValue(automation.raw.executor, 'message');
      ticketDraft = firstTicketBody(automation.raw.executor);
      if (automation.executorKind === 'pma_turn') {
        selectedAgent = stringValue(automation.raw.executor, 'agent') || defaultAgentId || agentIdFallback();
        selectedProfile = stringValue(automation.raw.executor, 'profile') || stringValue(automation.raw.executor, 'agent_profile');
        selectedModel = stringValue(automation.raw.executor, 'model');
        selectedReasoning = stringValue(automation.raw.executor, 'reasoning');
        void loadModels(selectedAgent, selectedModel, { keepReasoning: true });
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
    detailName = preset.name;
    detailEnabled = false;
    detailHour = preset.defaultHour;
    detailMinute = 0;
    detailWeekday = preset.defaultWeekday ?? 0;
    if (preset.executorKind === 'pma_turn') {
      selectedAgent = defaultAgentId || agentIdFallback();
      selectedProfile = selectedAgent === 'hermes' ? defaultProfile : '';
      selectedModel = '';
      selectedReasoning = '';
      void loadModels(selectedAgent, '', { keepReasoning: false });
    } else {
      clearAgentModelSelection();
    }
    promptDraft = preset.prompt;
    ticketDraft = preset.ticketBody ?? '';
    triggerDraft = prettyJson({ event_types: ['schedule.fire'] });
    filtersDraft = prettyJson({ schedule: { rule_id: '<assigned on save>' } });
    targetDraft = prettyJson(preset.id === 'weekly_ticket_flow' ? { base_repo_id: selectedRepoId } : { repo_id: selectedRepoId });
    executorDraft = prettyJson(preset.id === 'weekly_ticket_flow' ? { ticket_pack: { source: 'inline', tickets: [{ path: 'TICKET-001.md', content: ticketDraft }] } } : { lane_id: 'pma:default', message: promptDraft });
    policyDraft = prettyJson({ approval_mode: 'never_require_approval', max_attempts: preset.id === 'weekly_ticket_flow' ? 2 : 3 });
    metadataDraft = prettyJson({ preset: preset.id, automation_kind: preset.kind, repo_id: selectedRepoId });
  }

  function selectAutomation(automation: AutomationSummary): void {
    selectedKind = 'automation';
    selectedId = automation.id;
    notice = null;
  }

  function selectPreset(preset: AutomationPreset): void {
    selectedKind = 'preset';
    selectedId = preset.id;
    notice = null;
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
        failedJobs: nextAutomations.filter((automation) => automation.lastJob?.state === 'failed').length
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
    if (selectedKind !== 'automation') return;
    if (saveTimer) window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      saveTimer = null;
      void savePatch(patch, label);
    }, 500);
  }

  async function saveJsonField(field: JsonField, draft: string): Promise<void> {
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
    const executor = { ...asRecord(automation.raw.executor) };
    const ticketPack: JsonRecord = { ...asRecord(executor.ticket_pack), source: stringValue(executor.ticket_pack, 'source') || 'inline' };
    ticketPack.tickets = tickets.map((ticket) => ({ path: ticket.path, content: ticket.content }));
    executor.ticket_pack = ticketPack;
    executorDraft = prettyJson(executor);
    await savePatch({ executor }, 'ticket pack');
  }

  async function createPresetAutomation(): Promise<void> {
    if (!selectedRepoId || saving) return;
    const preset = selectedPreset();
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
      agent: preset.executorKind === 'pma_turn' ? selectedAgent || null : null,
      model: preset.executorKind === 'pma_turn' ? selectedModel || null : null,
      reasoning: preset.executorKind === 'pma_turn' ? selectedReasoning || null : null,
      profile: preset.executorKind === 'pma_turn' ? selectedProfile || null : null,
      enabled: detailEnabled
    });
    saving = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    overview = overview
      ? { ...overview, automations: [...overview.automations, result.data] }
      : { automations: [result.data], summary: { total: 1, active: result.data.enabled ? 1 : 0, paused: result.data.enabled ? 0 : 1, failedJobs: 0 } };
    selectedKind = 'automation';
    selectedId = result.data.id;
    notice = `Created ${result.data.name}${result.data.enabled ? '' : ' — paused'}`;
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
      const agentLines =
        automation.executorKind === 'pma_turn'
          ? [
              `Agent: ${selectedAgent || '(default)'}`,
              `Model: ${selectedModel || '(default)'}`,
              `Reasoning: ${selectedReasoning || '(default)'}`,
              `Profile: ${selectedProfile || '(default)'}`
            ]
          : ['Ticket-flow agent/model assignment is controlled by ticket frontmatter.'];
      return [
        `I want to edit automation ${automation.id}.`,
        '',
        `Name: ${automation.name}`,
        `Kind: ${automation.kind}`,
        `Executor: ${automation.executorKind}`,
        ...agentLines,
        `Target policy: ${automation.targetPolicy}`,
        `Schedule: ${scheduleLabel(automation)}`,
        '',
        'Current prompt or ticket body:',
        promptDraft || ticketDraft || '(none)',
        '',
        'Help me make the right semantic changes, then tell me what direct fields to update in the automation detail view.'
      ].join('\n');
    }
    const preset = selectedPreset();
    const agentLines =
      preset.executorKind === 'pma_turn'
        ? [
            `Agent: ${selectedAgent || '(default)'}`,
            `Model: ${selectedModel || '(default)'}`,
            `Reasoning: ${selectedReasoning || '(default)'}`
          ]
        : ['Ticket-flow agent/model assignment should stay in ticket frontmatter.'];
    return [
      `I want to create an automation based on the ${preset.name} preset.`,
      '',
      `Repo: ${(selectedRepo?.name ?? selectedRepoId) || '(choose repo)'}`,
      ...agentLines,
      `Schedule: ${preset.scheduleKind} at ${pad(detailHour)}:${pad(detailMinute)} ${timezone}`,
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
    const schedule = automation.schedule;
    if (!schedule) return 'Event-driven';
    const hour = schedule.schedule.hour;
    const minute = schedule.schedule.minute;
    const time = `${pad(Number(hour ?? 0))}:${pad(Number(minute ?? 0))}`;
    if (schedule.scheduleKind === 'weekly') {
      const weekdays = Array.isArray(schedule.schedule.weekdays) ? schedule.schedule.weekdays : [0];
      const day = weekdayLabel(Number(weekdays[0] ?? 0), true);
      return `${day} ${time} ${schedule.timezone}`;
    }
    if (schedule.scheduleKind === 'daily') return `Daily ${time} ${schedule.timezone}`;
    return `${schedule.scheduleKind} ${schedule.timezone}`;
  }

  function targetLabel(automation: AutomationSummary | null): string {
    if (!automation) return selectedRepo?.name || selectedRepoId || 'Hub';
    const repoId = String(automation.target.repo_id ?? automation.target.base_repo_id ?? '');
    if (repoId) {
      const repo = repos.find((entry) => entry.id === repoId);
      return repo?.name || repoId;
    }
    return String(automation.target.worktree_id ?? 'Hub');
  }

  function selectedScheduleKind(): string {
    return selectedAutomation()?.schedule?.scheduleKind ?? selectedPreset().scheduleKind;
  }

  function selectedExecutorKind(): string {
    return selectedAutomation()?.executorKind ?? selectedPreset().executorKind;
  }

  function selectedTargetPolicy(): string {
    return selectedAutomation()?.targetPolicy ?? selectedPreset().targetPolicy;
  }

  function detailDescription(): string {
    const automation = selectedAutomation();
    if (automation) return String(automation.metadata.description ?? kindLabel(automation.kind));
    return selectedPreset().description;
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
    if (selectedPreset().executorKind !== 'ticket_flow') return [];
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
    if (selectedExecutorKind() !== 'pma_turn') return;
    void savePatch(
      {
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
    if (selectedKind === 'automation') void savePatch(schedulePatch(), 'schedule');
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
    pma_turn: 'PMA turn',
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
    if (automation.lastJob?.state === 'failed') return { key: 'failed', dot: 'status-failed', label: 'failed' };
    if (automation.enabled) return { key: 'active', dot: 'status-running', label: 'active' };
    return { key: 'paused', dot: 'status-idle', label: 'paused' };
  }

  function hasSchedule(): boolean {
    return selectedKind === 'preset' || Boolean(selectedAutomation()?.schedule);
  }

  function runsAsLabel(): string {
    if (selectedExecutorKind() === 'pma_turn') {
      return selectedModel ? `${selectedAgent || 'default'} · ${selectedModel}` : selectedAgent || 'default agent';
    }
    if (selectedExecutorKind() === 'ticket_flow') return 'Ticket flow';
    return kindLabel(selectedExecutorKind());
  }

  function scheduleSummary(): string {
    const automation = selectedAutomation();
    if (automation) return scheduleLabel(automation);
    const preset = selectedPreset();
    const time = `${pad(detailHour)}:${pad(detailMinute)}`;
    if (preset.scheduleKind === 'weekly') return `${weekdayLabel(detailWeekday, true)} ${time} ${timezone}`;
    return `Daily ${time} ${timezone}`;
  }
</script>

{#snippet heroActions()}
  <button type="button" class="primary-button" onclick={() => openPmaWithDraft(createNewAutomationPrompt())}>Create with PMA</button>
  <button type="button" class="hero-action" onclick={() => void load()} disabled={loading} aria-label="Refresh automations">Refresh</button>
{/snippet}

{#snippet heroStats()}
  {#if overview}
    <dl class="hero-stats">
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
{/snippet}

<svelte:head>
  <title>Automations</title>
</svelte:head>

<section class="page-stack automations-page">
  <PageHero
    title="Automations"
    subtitle="Scheduled and event-driven PMA runs. Create them with PMA, tune saved rules here."
    stats={heroStats}
    actions={heroActions}
  />

  {#if error}
    <div class="automation-notice error" role="alert">{error.message}</div>
  {/if}
  {#if notice}
    <div class="automation-notice success" role="status">{notice}</div>
  {/if}

  <div class="automation-workbench">
    <aside class="automation-list" aria-label="Automations and presets">
      <div class="list-group-head">
        <h2>Saved</h2>
        <span>{automations.length} {automations.length === 1 ? 'rule' : 'rules'}</span>
      </div>
      {#if loading}
        <div class="state-panel loading-state"><p>Loading automations…</p></div>
      {:else if automations.length === 0}
        <div class="state-panel empty-state"><p>No saved automations yet. Pick a preset below or create one with PMA.</p></div>
      {:else}
        <ul class="card-list">
          {#each automations as automation}
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
                    <span>{scheduleLabel(automation)}</span>
                    <span class="meta-dot">·</span>
                    <span class="kind-chip">{kindLabel(automation.kind)}</span>
                  </span>
                </span>
                <span class="card-chevron" aria-hidden="true">→</span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="list-group-head presets-head">
        <h2>Start from a preset</h2>
      </div>
      <ul class="card-list">
        {#each PRESETS as preset}
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
                  <span>{preset.scheduleKind}</span>
                  <span class="meta-dot">·</span>
                  <span class="kind-chip">{kindLabel(preset.executorKind)}</span>
                </span>
              </span>
              <span class="card-chevron" aria-hidden="true">→</span>
            </button>
          </li>
        {/each}
      </ul>
    </aside>

    <section class="automation-detail" aria-label="Automation detail">
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
              disabled={actionId === selectedAutomation()?.id}
              onclick={() => selectedAutomation() && runNow(selectedAutomation() as AutomationSummary)}
            >Run now</button>
            <button
              type="button"
              class="ghost-button"
              disabled={actionId === selectedAutomation()?.id}
              onclick={() => selectedAutomation() && setEnabled(selectedAutomation() as AutomationSummary, !detailEnabled)}
            >{detailEnabled ? 'Pause' : 'Resume'}</button>
            <button type="button" class="ghost-button" onclick={() => openPmaWithDraft(editWithPmaPrompt())}>Edit with PMA</button>
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
      {/if}

      <!-- At a glance: read-only runtime facts -->
      <dl class="fact-grid">
        <div class="fact">
          <dt>Schedule</dt>
          <dd>{scheduleSummary()}</dd>
        </div>
        <div class="fact">
          <dt>Runs as</dt>
          <dd>{runsAsLabel()}</dd>
        </div>
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
            <dd class="fact-status fact-{selectedAutomation()?.lastJob?.state ?? 'none'}">
              {selectedAutomation()?.lastJob?.state ?? 'No runs yet'}
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
            <input bind:value={detailName} oninput={() => saveTextDebounced({ name: detailName }, 'name')} />
          </label>

          {#if selectedKind === 'preset'}
            <label class="field">
              <span>Repo</span>
              <select bind:value={selectedRepoId}>
                {#each repos as repo}
                  <option value={repo.id}>{repo.name || repo.id}</option>
                {/each}
              </select>
            </label>
            <label class="field">
              <span>Start</span>
              <select bind:value={detailEnabled}>
                <option value={false}>Paused</option>
                <option value={true}>Active</option>
              </select>
            </label>
          {/if}

          {#if hasSchedule()}
            <label class="field">
              <span>Time</span>
              <input type="time" value={scheduleTimeValue} onchange={onScheduleTime} />
            </label>
            <label class="field">
              <span>Timezone</span>
              <input bind:value={timezone} onchange={() => selectedKind === 'automation' && void savePatch(schedulePatch(), 'schedule')} />
            </label>
            {#if selectedScheduleKind() === 'weekly'}
              <label class="field">
                <span>Day</span>
                <select bind:value={detailWeekday} onchange={() => selectedKind === 'automation' && void savePatch(schedulePatch(), 'schedule')}>
                  {#each [0, 1, 2, 3, 4, 5, 6] as day}
                    <option value={day}>{weekdayLabel(day)}</option>
                  {/each}
                </select>
              </label>
            {/if}
          {/if}
        </div>

        {#if selectedExecutorKind() === 'pma_turn'}
          <div class="agent-picker-row">
            <AgentModelReasoningPicker
              {agents}
              bind:agentValue={selectedAgent}
              bind:profileValue={selectedProfile}
              bind:modelValue={selectedModel}
              bind:reasoningValue={selectedReasoning}
              {models}
              loading={loadingModels}
              {modelCatalogError}
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
            onChange={saveTicketPack}
            allowAddRemove={selectedKind === 'automation'}
          />
        {:else}
          <h3>Prompt</h3>
          <textarea
            class="instruction-editor"
            bind:value={promptDraft}
            oninput={() => saveTextDebounced({ prompt: promptDraft }, 'prompt')}
          ></textarea>
        {/if}
      </div>

      {#if selectedAutomation()}
        <div class="detail-section">
          <h3>Run history</h3>
          <RunHistoryList runs={selectedRunHistory()} emptyMessage="This automation has not run yet." />
        </div>
      {/if}

      <!-- Advanced: raw rule config, normally authored by PMA -->
      <details class="advanced">
        <summary>
          <span>Advanced configuration</span>
          <span class="advanced-hint">Authored by PMA — edit only if you know the rule schema</span>
        </summary>
        <div class="json-grid">
          <label class="field">
            <span>Trigger</span>
            <textarea class="json-editor" bind:value={triggerDraft} onblur={() => void saveJsonField('trigger', triggerDraft)}></textarea>
          </label>
          <label class="field">
            <span>Filters</span>
            <textarea class="json-editor" bind:value={filtersDraft} onblur={() => void saveJsonField('filters', filtersDraft)}></textarea>
          </label>
          <label class="field">
            <span>Target</span>
            <textarea class="json-editor" bind:value={targetDraft} onblur={() => void saveJsonField('target', targetDraft)}></textarea>
          </label>
          <label class="field">
            <span>Executor</span>
            <textarea class="json-editor" bind:value={executorDraft} onblur={() => void saveJsonField('executor', executorDraft)}></textarea>
          </label>
          <label class="field">
            <span>Policy</span>
            <textarea class="json-editor" bind:value={policyDraft} onblur={() => void saveJsonField('policy', policyDraft)}></textarea>
          </label>
          <label class="field">
            <span>Metadata</span>
            <textarea class="json-editor" bind:value={metadataDraft} onblur={() => void saveJsonField('metadata', metadataDraft)}></textarea>
          </label>
        </div>
      </details>
    </section>
  </div>
</section>

<style>
  .automations-page {
    padding: 0 var(--space-6) var(--space-8);
    gap: var(--space-4);
  }

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

  .automation-workbench {
    display: grid;
    grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
    gap: var(--space-4);
    align-items: start;
  }

  /* ---- List column ---- */
  .automation-list {
    display: grid;
    gap: var(--space-2);
    align-content: start;
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

  .presets-head {
    margin-top: var(--space-3);
  }

  .card-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-2);
  }

  .automation-card {
    position: relative;
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
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
    box-shadow: 0 8px 24px -16px rgb(15 15 20 / 0.18), 0 2px 6px -3px rgb(15 15 20 / 0.06);
  }

  .automation-card.selected {
    border-color: var(--color-accent);
    box-shadow: inset 0 0 0 1px var(--color-accent);
  }

  .automation-avatar {
    flex: 0 0 auto;
    width: 36px;
    height: 36px;
    border-radius: 10px;
    display: grid;
    place-items: center;
    font-size: var(--font-size-1);
    font-weight: 650;
    color: var(--accent, var(--color-accent));
    background: color-mix(in srgb, var(--accent, var(--color-accent)) 12%, white);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent, var(--color-accent)) 18%, transparent);
  }

  .preset-avatar {
    --accent: var(--color-ink-muted);
    font-size: var(--font-size-3);
    font-weight: 500;
  }

  .automation-card-body {
    flex: 1 1 auto;
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .automation-card-title {
    font-size: var(--font-size-2);
    font-weight: 600;
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .automation-card-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 5px;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
  }

  .meta-dot {
    color: var(--color-ink-faint);
    opacity: 0.7;
  }

  .kind-chip {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    padding: 1px 6px;
    border-radius: 4px;
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
    display: grid;
    gap: var(--space-4);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
    padding: var(--space-5);
    min-width: 0;
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

  .detail-banner {
    margin: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink-soft);
    background: var(--color-surface-sunken);
    border-radius: var(--radius-2);
    padding: var(--space-2) var(--space-3);
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

  .instruction-editor {
    min-height: 150px;
    line-height: 1.5;
    resize: vertical;
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

  .json-editor {
    font-family: var(--font-mono);
    font-size: var(--font-size-0);
    min-height: 150px;
    line-height: 1.45;
    resize: vertical;
    background: var(--color-surface-sunken);
  }

  @media (max-width: 1020px) {
    .automation-workbench {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 760px) {
    .automations-page {
      padding: 0 var(--space-4) var(--space-6);
    }

    .automation-detail {
      padding: var(--space-4);
    }

    .detail-actions {
      width: 100%;
    }
  }
</style>
