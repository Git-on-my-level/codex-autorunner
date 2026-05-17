<script lang="ts">
  import type {
    SettingsAgentStatus,
    SettingsSessionState,
    SettingsStatusItem,
    SettingsViewModel
  } from '$lib/viewModels/settings';
  import PageHero from './PageHero.svelte';
  import AutoDismissNotice from './AutoDismissNotice.svelte';
  import DropdownSelect, { type DropdownSelectGroup, type DropdownSelectOption } from './DropdownSelect.svelte';
  import {
    applyThemePreference,
    isThemePreference,
    readStoredThemePreference,
    type ThemePreference
  } from '$lib/theme';
  import { onMount, untrack } from 'svelte';

  let {
    state: viewState,
    view = null,
    sessionBaselineEpoch = 0,
    errorMessage = null,
    saveError = null,
    saving = false,
    onSessionChange,
    onSavePreferences,
    onOpenPmaMemory,
    onOpenSetupChat
  }: {
    state: 'loading' | 'error' | 'ready';
    view?: SettingsViewModel | null;
    errorMessage?: string | null;
    saveError?: string | null;
    saving?: boolean;
    onSessionChange?: (session: SettingsSessionState) => void;
    onSavePreferences?: () => void;
    onOpenPmaMemory?: () => void;
    onOpenSetupChat?: (kind: SetupPromptKind) => void;
    sessionBaselineEpoch?: number;
  } = $props();

  type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

  let savedSession: SettingsSessionState | null = $state(null);
  let themePreference = $state<ThemePreference>('system');

  onMount(() => {
    themePreference = readStoredThemePreference();
  });

  function pickThemePreference(pref: ThemePreference): void {
    themePreference = pref;
    applyThemePreference(pref);
  }

  const themeGroups: DropdownSelectGroup[] = [
    {
      label: 'PMA Hub default',
      options: [
        { value: 'system', label: 'System (match OS)' },
        { value: 'light', label: 'Light' },
        { value: 'dark', label: 'Dark' }
      ]
    },
    {
      label: 'Solarized',
      options: [
        { value: 'solarized-light', label: 'Solarized Light' },
        { value: 'solarized-dark', label: 'Solarized Dark' }
      ]
    },
    {
      label: 'IDE-style',
      options: [
        { value: 'dracula', label: 'Dracula' },
        { value: 'nord', label: 'Nord' },
        { value: 'one-dark', label: 'One Dark' },
        { value: 'github-light', label: 'GitHub Light' },
        { value: 'github-dark', label: 'GitHub Dark' }
      ]
    }
  ];
  const approvalPolicyOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'never', label: 'never' },
    { value: 'unlessTrusted', label: 'unlessTrusted' }
  ];
  const sandboxModeOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'dangerFullAccess', label: 'dangerFullAccess' },
    { value: 'workspaceWrite', label: 'workspaceWrite' }
  ];
  const workspaceWriteNetworkOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'true', label: 'Enabled' },
    { value: 'false', label: 'Disabled' }
  ];

  function onThemeSelectChange(value: string): void {
    if (!isThemePreference(value)) return;
    pickThemePreference(value);
  }

  $effect(() => {
    sessionBaselineEpoch;
    const snapshot = untrack(() => view);
    if (snapshot) savedSession = { ...snapshot.session };
  });

  const sessionDirty = $derived.by(() => {
    if (!view || !savedSession) return false;
    const baseline = savedSession;
    return (
      JSON.stringify(view.session.modelOverrides) !== JSON.stringify(baseline.modelOverrides) ||
      view.session.effortOverride !== baseline.effortOverride ||
      view.session.stopAfterRuns !== baseline.stopAfterRuns ||
      view.session.approvalPolicy !== baseline.approvalPolicy ||
      view.session.sandboxMode !== baseline.sandboxMode ||
      view.session.workspaceWriteNetwork !== baseline.workspaceWriteNetwork
    );
  });

  // Surface only the *interesting* hub rows: a degraded runtime API. The
  // canonical-default rows (Hub mode = local, Settings changes = direct save)
  // communicate "nothing is happening" and are suppressed per DESIGN.md.
  const degradedHub = $derived.by<SettingsStatusItem[]>(() => {
    if (!view) return [];
    return view.hub.filter((item) => item.tone === 'warning');
  });

  function patchSession(key: keyof SettingsSessionState, value: SettingsSessionState[keyof SettingsSessionState]): void {
    if (!view) return;
    onSessionChange?.({ ...view.session, [key]: value });
  }

  function patchAgentModel(agentId: string, value: string): void {
    if (!view) return;
    const agent = agentId.trim().toLowerCase();
    const next = { ...view.session.modelOverrides };
    const model = value.trim();
    if (model) {
      next[agent] = model;
    } else {
      delete next[agent];
    }
    onSessionChange?.({ ...view.session, modelOverrides: next });
  }

  function selectedAgentModel(agent: SettingsAgentStatus, session: SettingsSessionState): string {
    const selected = session.modelOverrides[agent.id] ?? '';
    return agent.modelOptions.some((model) => model.id === selected) ? selected : '';
  }

  function agentModelOptions(agent: SettingsAgentStatus): DropdownSelectOption[] {
    return [
      { value: '', label: 'Use built-in default' },
      ...agent.modelOptions.map((model) => ({ value: model.id, label: model.label }))
    ];
  }

  function patchNetwork(value: string): void {
    patchSession('workspaceWriteNetwork', value === '' ? null : value === 'true');
  }
</script>

<section class="page-stack settings-page">
  <PageHero title="Settings" subtitle="Hub, agents, and CAR session preferences.">
    {#snippet stats()}
      {#if sessionDirty}
        <dl class="hero-stats" aria-label="Settings status">
          <div class="waiting">
            <dd>Unsaved</dd>
            <dt>Preferences</dt>
          </div>
        </dl>
      {/if}
    {/snippet}
  </PageHero>

  {#if viewState === 'loading'}
    <div class="state-panel loading-state">Loading settings…</div>
  {:else if viewState === 'error'}
    <div class="state-panel error">Could not load settings. {errorMessage}</div>
  {:else if view}
    {#if degradedHub.length > 0}
      <div class="state-panel error" role="status">
        {#each degradedHub as item}
          <div><strong>{item.label}:</strong> {item.value}</div>
        {/each}
      </div>
    {/if}

    <button type="button" class="memory-card" onclick={() => onOpenPmaMemory?.()}>
      <span class="memory-card-glyph" aria-hidden="true">M</span>
      <span class="memory-card-copy">
        <strong>PMA memory</strong>
        <span>Hub-wide notes the agent reads at the start of every chat — instructions, conventions, and durable context.</span>
      </span>
      <span class="memory-card-chevron" aria-hidden="true">›</span>
    </button>

    <section class="settings-section">
      <h2 class="settings-section-title">Appearance</h2>
      <DropdownSelect
        value={themePreference}
        groups={themeGroups}
        labelText="Theme"
        ariaLabel="Color theme"
        rowClass="theme-select-field"
        searchable={true}
        searchPlaceholder="Search themes"
        onchange={onThemeSelectChange}
      />
    </section>

    <section class="settings-section">
      <h2 class="settings-section-title">Setup with PMA</h2>
      <div class="settings-action-grid">
        <button type="button" class="setup-action" onclick={() => onOpenSetupChat?.('telegram')}>
          <strong>Telegram</strong>
          <span>Interactive mobile chat, topics, allowlists</span>
        </button>
        <button type="button" class="setup-action" onclick={() => onOpenSetupChat?.('discord')}>
          <strong>Discord</strong>
          <span>Slash commands, PMA mode, voice, channels</span>
        </button>
        <button type="button" class="setup-action" onclick={() => onOpenSetupChat?.('notifications')}>
          <strong>Notifications</strong>
          <span>Run finished, run error, idle alerts</span>
        </button>
        <button type="button" class="setup-action" onclick={() => onOpenSetupChat?.('github')}>
          <strong>GitHub automation</strong>
          <span>Webhooks, PR bindings, review workflows</span>
        </button>
      </div>
    </section>

    <section class="settings-section">
      <div class="settings-section-head">
        <h2 class="settings-section-title">Runner overrides</h2>
        <button
          type="button"
          class="ghost-button"
          class:dirty={sessionDirty}
          disabled={!sessionDirty || saving}
          onclick={() => onSavePreferences?.()}
        >
          {saving ? 'Saving…' : sessionDirty ? 'Save preferences' : 'Saved'}
        </button>
      </div>
      <div class="settings-form-grid">
        <label>
          <span>Reasoning override</span>
          <input
            value={view.session.effortOverride}
            placeholder="Use agent default"
            oninput={(event) => patchSession('effortOverride', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Stop after runs</span>
          <input
            inputmode="numeric"
            value={view.session.stopAfterRuns}
            placeholder="No limit"
            oninput={(event) => patchSession('stopAfterRuns', event.currentTarget.value)}
          />
        </label>
        <DropdownSelect
          value={view.session.approvalPolicy}
          options={approvalPolicyOptions}
          labelText="Approval policy"
          ariaLabel="Approval policy"
          onchange={(value) => patchSession('approvalPolicy', value)}
        />
        <DropdownSelect
          value={view.session.sandboxMode}
          options={sandboxModeOptions}
          labelText="Sandbox mode"
          ariaLabel="Sandbox mode"
          onchange={(value) => patchSession('sandboxMode', value)}
        />
        <DropdownSelect
          value={view.session.workspaceWriteNetwork === null ? '' : String(view.session.workspaceWriteNetwork)}
          options={workspaceWriteNetworkOptions}
          labelText="Workspace-write network"
          ariaLabel="Workspace-write network"
          onchange={patchNetwork}
        />
      </div>
      <AutoDismissNotice message={saveError} tone="danger" />
    </section>

    <section class="settings-section">
      <h2 class="settings-section-title">Agents</h2>
      {@render agentList(view.agents, view.session, 'No agents are visible from the server.')}
    </section>

    <section class="settings-section">
      <div class="settings-section-head">
        <h2 class="settings-section-title">Voice transcription</h2>
        <div class="settings-section-actions">
          {#if view.voice.enabled}
            <span class="status-pill done">enabled</span>
          {:else}
            <span class="status-pill waiting">disabled</span>
            <button type="button" class="ghost-button" onclick={() => onOpenSetupChat?.('voice')}>
              Enable with PMA
            </button>
          {/if}
        </div>
      </div>
      {@render statusList(view.voice.rows)}
      {#if view.voice.hint}
        <p class="voice-hint">{view.voice.hint}</p>
      {/if}
      {#if !view.voice.enabled && view.voice.apiKeyEnv}
        <p class="voice-hint voice-hint-cmd">
          Set the env var, then restart the hub:
          <code>export {view.voice.apiKeyEnv}=…</code>
        </p>
      {/if}
    </section>
  {/if}
</section>

{#snippet statusList(items: SettingsStatusItem[])}
  <dl class="settings-status-list">
    {#each items as item}
      <div class={item.tone}>
        <dt>{item.label}</dt>
        <dd>{item.value}</dd>
      </div>
    {/each}
  </dl>
{/snippet}

{#snippet agentList(agents: SettingsAgentStatus[], session: SettingsSessionState, emptyText: string)}
  {#if agents.length === 0}
    <div class="state-panel empty-state compact-empty">
      <strong>No agents visible</strong>
      <p>{emptyText}</p>
    </div>
  {:else}
    <div class="agent-status-list">
      {#each agents as agent}
        <article class="agent-status-row">
          <div class="agent-status-id">
            <strong>{agent.name}</strong>
          </div>
          {#if agent.modelStatus === 'available'}
            <div class="agent-model-control">
              <DropdownSelect
                value={selectedAgentModel(agent, session)}
                options={agentModelOptions(agent)}
                labelText="Default model"
                ariaLabel={`${agent.name} default model`}
                searchable={agent.modelOptions.length > 8}
                searchPlaceholder="Search models"
                onchange={(value) => patchAgentModel(agent.id, value)}
              />
            </div>
          {:else if agent.modelStatus === 'unavailable'}
            <span class="agent-model-muted">Models unavailable</span>
          {:else}
            <span class="agent-model-muted">Model selection unavailable</span>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
{/snippet}

<style>
  .memory-card {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
    color: var(--color-ink);
    text-align: left;
    cursor: pointer;
    transition:
      border-color var(--transition-fast) var(--ease-out),
      box-shadow var(--transition-fast) var(--ease-out);
  }

  .memory-card:hover {
    border-color: var(--color-border-strong);
    box-shadow:
      0 8px 24px -16px rgb(15 15 20 / 0.18),
      0 2px 6px -3px rgb(15 15 20 / 0.06);
  }

  .memory-card:hover .memory-card-chevron {
    color: var(--color-ink-soft);
    transform: translateX(2px);
  }

  .memory-card:focus-visible {
    outline: 2px solid var(--color-accent);
    outline-offset: 2px;
  }

  .memory-card-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 8px;
    background: var(--color-accent-soft, var(--color-surface-muted));
    color: var(--color-accent);
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-weight: 700;
    font-size: var(--font-size-2);
  }

  .memory-card-copy {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .memory-card-copy strong {
    font-size: var(--font-size-1);
    font-weight: 650;
  }

  .memory-card-copy span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.4;
  }

  .memory-card-chevron {
    color: var(--color-ink-faint);
    font-size: var(--font-size-3);
    line-height: 1;
    transition:
      color var(--transition-fast) var(--ease-out),
      transform var(--transition-base) var(--ease-out);
  }

  .agent-status-id strong {
    font-size: var(--font-size-1);
    font-weight: 600;
  }

  .settings-action-grid {
    display: grid;
    /* Tight min so 4 cards fit one row at wide widths, then reflow to 2x2,
       then 1-col. Avoids the 3+1 stranded-card layout. */
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
    gap: var(--space-3);
  }

  @media (max-width: 760px) {
    .settings-action-grid {
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 160px), 1fr));
    }
  }

  .setup-action {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
    color: var(--color-ink);
    text-align: left;
    cursor: pointer;
    transition:
      border-color var(--transition-fast) var(--ease-out),
      background var(--transition-fast) var(--ease-out);
  }

  .setup-action:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
  }

  .setup-action:focus-visible {
    outline: 2px solid var(--color-accent);
    outline-offset: 2px;
  }

  .setup-action strong {
    font-size: var(--font-size-1);
    font-weight: 650;
  }

  .setup-action span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.35;
  }

  .voice-hint {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.5;
  }

  .voice-hint-cmd code {
    display: inline-block;
    margin-top: 4px;
    padding: 2px 8px;
    border-radius: 6px;
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-0);
  }
</style>
