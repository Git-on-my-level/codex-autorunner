<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import { page } from '$app/state';
  import {
    webApi,
    type ApiError,
    type JsonRecord,
    type SystemUpdateStatus,
    type SystemUpdateTargetOption
  } from '$lib/api/client';
  import {
    agentModelCatalogStore,
    type AgentModelCatalogState
  } from '$lib/application/agentModelCatalogStore';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import SettingsView from '$lib/components/SettingsView.svelte';
  import {
    isSettingsSectionId,
    type SettingsSectionId
  } from '$lib/components/settings/sections';
  import {
    buildSessionUpdatePayload,
    buildSettingsViewModel,
    type SettingsSessionState,
    type SettingsViewModel
  } from '$lib/viewModels/settings';
  import { buildMemoryViewModel, type MemoryViewModel } from '$lib/viewModels/memory';
  import { renderMarkdownToHtml } from '$lib/viewModels/markdown';
  import { formatScopeUrn } from '$lib/viewModels/scope';

  let view = $state<SettingsViewModel | null>(null);
  let sessionBaselineEpoch = $state(0);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let saveError = $state<ApiError | null>(null);
  let saving = $state(false);
  let settingsSessionRaw = $state<JsonRecord | null>(null);
  let settingsVoiceRaw = $state<JsonRecord | null>(null);
  let updateTargets = $state<SystemUpdateTargetOption[]>([]);
  let selectedUpdateTarget = $state('');
  let updateStatus = $state<SystemUpdateStatus | null>(null);
  let updateLoading = $state(false);
  let updateStarting = $state(false);
  let updateMessage = $state<string | null>(null);
  let updateError = $state<string | null>(null);
  let pendingUpdateConfirmation = $state(false);
  let unsubscribeCatalog: (() => void) | null = null;

  let memoryVm = $state<MemoryViewModel | null>(null);
  let memoryLoading = $state(false);
  let memoryError = $state<ApiError | null>(null);
  let memoryLoadedScopeUrn: string | null = null;
  let memoryLoadSeq = 0;

  const hubScope = { kind: 'hub' as const };
  type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

  const sectionId = $derived<SettingsSectionId | null>(
    isSettingsSectionId(page.params.sectionId) ? page.params.sectionId : null
  );

  const memoryState = $derived<'loading' | 'error' | 'ready'>(
    memoryLoading ? 'loading' : memoryError ? 'error' : 'ready'
  );

  const setupPrompts: Record<SetupPromptKind, string> = {
    telegram:
      'Walk me through setting up Telegram for this Web Hub. Please inspect the current hub config, ask for any missing bot/token/chat details, and make the smallest safe config changes. Do not start or restart services without asking me first.',
    discord:
      'Walk me through setting up Discord for this Web Hub. Please inspect the current hub config, cover slash commands, PMA mode, allowlists, and voice/media options, and make the smallest safe config changes. Do not start or restart services without asking me first.',
    notifications:
      'Help me configure Web Hub notifications for run_finished, run_error, and tui_idle. Please inspect the current notification config, explain the available Discord and Telegram delivery options, and make the smallest safe config changes.',
    github:
      'Help me configure GitHub automation for this Web Hub. Please inspect the current config and explain webhook, PR binding, and review workflow options before making changes.',
    voice:
      'Walk me through enabling voice transcription for this Web Hub. Please inspect the current voice config, confirm whether local Whisper or a remote provider is appropriate, check any required runtime extras or API key env vars, and make the smallest safe config changes. Do not start or restart services without asking me first.'
  };

  onMount(() => {
    // Legacy ?memory=1 deep link → /settings/memory.
    if (page.url.searchParams.get('memory') === '1') {
      void goto(href('/settings/memory'), { replaceState: true });
    } else if (page.params.sectionId === 'system') {
      // Legacy /settings/system → folded into /settings/general.
      void goto(href('/settings/general'), { replaceState: true });
    } else if (!isSettingsSectionId(page.params.sectionId)) {
      void goto(href('/settings/general'), { replaceState: true });
    }
    unsubscribeCatalog = agentModelCatalogStore.subscribe((state) => {
      rebuildSettingsView(state);
    });
    void loadSettings();
    void loadSystemUpdateState();
  });

  onDestroy(() => {
    unsubscribeCatalog?.();
  });

  $effect(() => {
    if (sectionId === 'memory') void loadMemory();
  });

  async function loadSettings(): Promise<void> {
    loading = true;
    error = null;
    saveError = null;
    const catalogPromise = agentModelCatalogStore.ensureLoaded();
    const [session, voice] = await Promise.all([
      webApi.settings.getSession(),
      webApi.voice.getConfig()
    ]);

    const latestCatalog = !session.ok && !voice.ok ? await catalogPromise : agentModelCatalogStore.snapshot();

    settingsSessionRaw = session.ok ? session.data : null;
    settingsVoiceRaw = voice.ok ? voice.data : null;
    if (!settingsSessionRaw && latestCatalog.status === 'error' && !settingsVoiceRaw) {
      error = session.ok ? latestCatalog.error : session.error;
      loading = false;
      return;
    }
    rebuildSettingsView(latestCatalog);
    sessionBaselineEpoch += 1;
    loading = false;
    void catalogPromise;
  }

  function rebuildSettingsView(catalog: AgentModelCatalogState): void {
    if (!settingsSessionRaw && !settingsVoiceRaw && !view) return;
    const currentSession = view ? buildSessionUpdatePayload(view.session) : settingsSessionRaw;
    view = buildSettingsViewModel({
      session: currentSession,
      agents: catalog.agents,
      modelCatalogs: catalog.modelCatalogs,
      voiceConfig: settingsVoiceRaw
    });
  }

  function updateSession(session: SettingsSessionState): void {
    if (!view) return;
    view = { ...view, session };
  }

  async function savePreferences(): Promise<void> {
    if (!view || saving) return;
    saving = true;
    saveError = null;
    const result = await webApi.settings.updateSession(buildSessionUpdatePayload(view.session));
    if (!result.ok) {
      saveError = result.error;
      saving = false;
      return;
    }
    await loadSettings();
    saving = false;
  }

  async function loadSystemUpdateState(): Promise<void> {
    updateLoading = true;
    updateError = null;
    const [targets, status] = await Promise.all([webApi.system.getUpdateTargets(), webApi.system.getUpdateStatus()]);
    if (targets.ok) {
      updateTargets = targets.data.targets;
      if (!selectedUpdateTarget) selectedUpdateTarget = targets.data.defaultTarget;
    } else {
      updateError = targets.error.message;
    }
    if (status.ok) {
      updateStatus = status.data;
    } else if (!updateError) {
      updateError = status.error.message;
    }
    updateLoading = false;
  }

  function selectUpdateTarget(target: string): void {
    selectedUpdateTarget = target;
    pendingUpdateConfirmation = false;
    updateMessage = null;
    updateError = null;
  }

  async function startSystemUpdate(force = false): Promise<void> {
    if (updateStarting) return;
    updateStarting = true;
    updateError = null;
    updateMessage = null;
    const result = await webApi.system.startUpdate({
      target: selectedUpdateTarget || null,
      force
    });
    if (!result.ok) {
      updateError = result.error.message;
      updateStarting = false;
      return;
    }
    updateMessage = result.data.message;
    pendingUpdateConfirmation = result.data.requiresConfirmation;
    if (!result.data.requiresConfirmation) {
      await loadSystemUpdateState();
    }
    updateStarting = false;
  }

  async function loadMemory(): Promise<void> {
    const scopeUrn = formatScopeUrn(hubScope);
    if (memoryLoadedScopeUrn === scopeUrn && memoryVm) return;
    const seq = ++memoryLoadSeq;
    memoryLoading = true;
    memoryError = null;
    const docs = await webApi.pma.listDocsWithContent();
    if (seq !== memoryLoadSeq) return;
    if (!docs.ok) {
      memoryError = docs.error;
      memoryVm = null;
      memoryLoading = false;
      return;
    }
    memoryVm = buildMemoryViewModel(hubScope, docs.data);
    memoryLoadedScopeUrn = scopeUrn;
    memoryLoading = false;
  }

  async function saveMemoryDoc(docId: string, content: string): Promise<boolean> {
    const result = await webApi.pma.updateDoc(docId, content);
    if (!result.ok) {
      memoryError = result.error;
      return false;
    }
    if (memoryVm) {
      memoryVm = {
        ...memoryVm,
        docs: memoryVm.docs.map((doc) =>
          doc.id === docId ? { ...doc, content, html: renderMarkdownToHtml(content), isMissing: !content.trim() } : doc
        ),
        presentCount: memoryVm.docs.filter((doc) =>
          doc.id === docId ? !!content.trim() : !doc.isMissing
        ).length
      };
    }
    return true;
  }

  function openSetupChat(kind: SetupPromptKind): void {
    const params = new URLSearchParams({
      new: 'local',
      draft: setupPrompts[kind]
    });
    void goto(href(`/chats?${params.toString()}`));
  }

  function navigateSection(id: SettingsSectionId): void {
    if (id === sectionId) return;
    void goto(href(`/settings/${id}`));
  }

  function navigateList(): void {
    void goto(href('/settings'));
  }
</script>

<SettingsView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {sectionId}
  {sessionBaselineEpoch}
  {view}
  errorMessage={error?.message ?? null}
  saveError={saveError?.message ?? null}
  {saving}
  {updateTargets}
  {selectedUpdateTarget}
  {updateStatus}
  {updateLoading}
  {updateStarting}
  {updateMessage}
  {updateError}
  {pendingUpdateConfirmation}
  {memoryState}
  memoryVm={memoryVm}
  memoryError={memoryError?.message ?? null}
  onSessionChange={updateSession}
  onSavePreferences={savePreferences}
  onSelectUpdateTarget={selectUpdateTarget}
  onRefreshUpdateStatus={loadSystemUpdateState}
  onStartUpdate={() => startSystemUpdate(false)}
  onConfirmUpdate={() => startSystemUpdate(true)}
  onOpenSetupChat={openSetupChat}
  onNavigateSection={navigateSection}
  onNavigateList={navigateList}
  onSaveMemoryDoc={saveMemoryDoc}
/>
