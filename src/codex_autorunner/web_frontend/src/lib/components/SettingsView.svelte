<script lang="ts">
  import type {
    SettingsSessionState,
    SettingsStatusItem,
    SettingsViewModel
  } from '$lib/viewModels/settings';
  import type { SystemUpdateStatus, SystemUpdateTargetOption } from '$lib/api/client';
  import type { MemoryViewModel } from '$lib/viewModels/memory';
  import { untrack } from 'svelte';
  import MasterDetail from './MasterDetail.svelte';
  import MemoryView from './MemoryView.svelte';
  import GeneralSection from './settings/GeneralSection.svelte';
  import IntegrationsSection from './settings/IntegrationsSection.svelte';
  import AgentsRunnerSection from './settings/AgentsRunnerSection.svelte';
  import SystemSection from './settings/SystemSection.svelte';
  import {
    SETTINGS_SECTIONS,
    settingsSectionLabel,
    type SettingsSectionId
  } from './settings/sections';

  type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

  let {
    state: viewState,
    view = null,
    sectionId = null,
    sessionBaselineEpoch = 0,
    errorMessage = null,
    saveError = null,
    saving = false,
    updateTargets = [],
    selectedUpdateTarget = '',
    updateStatus = null,
    updateLoading = false,
    updateStarting = false,
    updateMessage = null,
    updateError = null,
    pendingUpdateConfirmation = false,
    memoryState = 'loading',
    memoryVm = null,
    memoryError = null,
    onSessionChange,
    onSavePreferences,
    onSelectUpdateTarget,
    onRefreshUpdateStatus,
    onStartUpdate,
    onConfirmUpdate,
    onOpenSetupChat,
    onNavigateSection,
    onNavigateList,
    onSaveMemoryDoc
  }: {
    state: 'loading' | 'error' | 'ready';
    view?: SettingsViewModel | null;
    sectionId?: SettingsSectionId | null;
    errorMessage?: string | null;
    saveError?: string | null;
    saving?: boolean;
    updateTargets?: SystemUpdateTargetOption[];
    selectedUpdateTarget?: string;
    updateStatus?: SystemUpdateStatus | null;
    updateLoading?: boolean;
    updateStarting?: boolean;
    updateMessage?: string | null;
    updateError?: string | null;
    pendingUpdateConfirmation?: boolean;
    memoryState?: 'loading' | 'error' | 'ready';
    memoryVm?: MemoryViewModel | null;
    memoryError?: string | null;
    onSessionChange?: (session: SettingsSessionState) => void;
    onSavePreferences?: () => void;
    onSelectUpdateTarget?: (target: string) => void;
    onRefreshUpdateStatus?: () => void;
    onStartUpdate?: () => void;
    onConfirmUpdate?: () => void;
    onOpenSetupChat?: (kind: SetupPromptKind) => void;
    onNavigateSection?: (sectionId: SettingsSectionId) => void;
    onNavigateList?: () => void;
    onSaveMemoryDoc?: (docId: string, content: string) => Promise<boolean> | boolean;
    sessionBaselineEpoch?: number;
  } = $props();

  let savedSession: SettingsSessionState | null = $state(null);

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
      view.session.workspaceWriteNetwork !== baseline.workspaceWriteNetwork ||
      view.session.ticketFlowRequireCommit !== baseline.ticketFlowRequireCommit
    );
  });

  const degradedHub = $derived.by<SettingsStatusItem[]>(() => {
    if (!view) return [];
    return view.hub.filter((item) => item.tone === 'warning');
  });

  const masterDetailMode = $derived<'list' | 'detail'>(sectionId ? 'detail' : 'list');

  function selectSection(id: SettingsSectionId): void {
    onNavigateSection?.(id);
  }
</script>

<MasterDetail
  label="Settings"
  selected={sectionId !== null}
  mode={masterDetailMode}
  listLabel="Settings"
  detailLabel={sectionId ? settingsSectionLabel(sectionId) : 'Detail'}
  showSwitch={false}
  hideDetail={sectionId === null}
  onModeChange={(mode) => {
    if (mode === 'list') onNavigateList?.();
  }}
>
  {#snippet list()}
    <aside class="settings-rail" aria-label="Settings categories">
      {#if viewState === 'loading'}
        <div class="state-panel loading-state">Loading settings…</div>
      {:else if viewState === 'error'}
        <div class="state-panel error">Could not load settings. {errorMessage}</div>
      {:else}
        <nav class="settings-nav" aria-label="Settings sections">
          {#each SETTINGS_SECTIONS as section (section.id)}
            <button
              type="button"
              class="settings-nav-item"
              class:active={section.id === sectionId}
              onclick={() => selectSection(section.id)}
            >
              <span class="settings-nav-label">{section.label}</span>
              <span class="settings-nav-chevron" aria-hidden="true">›</span>
            </button>
          {/each}
        </nav>
      {/if}
    </aside>
  {/snippet}

  {#snippet detail()}
    <section class="settings-detail" aria-label="Settings detail">
      {#if viewState === 'ready' && view && sectionId}
        {#if sectionId === 'memory'}
          <MemoryView
            state={memoryState}
            vm={memoryVm}
            errorMessage={memoryError}
            onSaveDoc={onSaveMemoryDoc}
          />
        {:else if sectionId === 'general'}
          <GeneralSection />
          <SystemSection
            {degradedHub}
            {updateTargets}
            {selectedUpdateTarget}
            {updateStatus}
            {updateLoading}
            {updateStarting}
            {updateMessage}
            {updateError}
            {pendingUpdateConfirmation}
            {onSelectUpdateTarget}
            {onRefreshUpdateStatus}
            {onStartUpdate}
            {onConfirmUpdate}
          />
        {:else if sectionId === 'integrations'}
          <IntegrationsSection voice={view.voice} {onOpenSetupChat} />
        {:else if sectionId === 'agents'}
          <AgentsRunnerSection
            session={view.session}
            agents={view.agents}
            {sessionDirty}
            {saving}
            {saveError}
            {onSessionChange}
            {onSavePreferences}
          />
        {/if}
      {:else if viewState === 'loading'}
        <div class="state-panel loading-state">Loading settings…</div>
      {:else if viewState === 'error'}
        <div class="state-panel error">Could not load settings. {errorMessage}</div>
      {/if}
    </section>
  {/snippet}
</MasterDetail>

<style>
  .settings-rail {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-height: 0;
    overflow: auto;
    padding-block: var(--space-1);
  }

  .settings-detail {
    display: flex;
    flex-direction: column;
    gap: 0;
    min-width: 0;
    min-height: 0;
    overflow: auto;
    padding-block: var(--space-1);
  }

  @media (max-width: 1020px) {
    .settings-detail {
      padding-inline: var(--space-1);
    }
  }

  .settings-nav {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: var(--space-1);
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
  }

  .settings-nav-item {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    min-height: 32px;
    padding: var(--space-1) var(--space-2);
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--color-ink);
    text-align: left;
    cursor: pointer;
    transition: background var(--transition-fast) var(--ease-out);
  }

  .settings-nav-item:hover {
    background: var(--color-surface-muted);
  }

  .settings-nav-item:focus-visible {
    outline: 2px solid var(--color-accent);
    outline-offset: -2px;
  }

  .settings-nav-item.active {
    background: var(--color-surface-muted);
    box-shadow: inset 0 0 0 1px var(--color-border-subtle);
  }

  .settings-nav-item.active .settings-nav-label {
    font-weight: 600;
  }

  .settings-nav-label {
    font-size: var(--font-size-1);
    font-weight: 500;
  }

  .settings-nav-chevron {
    color: var(--color-ink-faint);
    font-size: var(--font-size-2);
    line-height: 1;
  }

  .settings-nav-item.active .settings-nav-chevron {
    color: var(--color-ink-muted);
  }
</style>
