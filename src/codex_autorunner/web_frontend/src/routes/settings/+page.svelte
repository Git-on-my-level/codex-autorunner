<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import { page } from '$app/state';
  import { webApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import {
    agentModelCatalogStore,
    type AgentModelCatalogState
  } from '$lib/application/agentModelCatalogStore';
  import MemoryRail from '$lib/components/MemoryRail.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import SettingsView from '$lib/components/SettingsView.svelte';
  import {
    buildSessionUpdatePayload,
    buildSettingsViewModel,
    type SettingsSessionState,
    type SettingsViewModel
  } from '$lib/viewModels/settings';

  let view = $state<SettingsViewModel | null>(null);
  let sessionBaselineEpoch = $state(0);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let saveError = $state<ApiError | null>(null);
  let saving = $state(false);
  let memoryOpen = $state(false);
  let settingsSessionRaw = $state<JsonRecord | null>(null);
  let settingsVoiceRaw = $state<JsonRecord | null>(null);
  let unsubscribeCatalog: (() => void) | null = null;
  const hubScope = { kind: 'hub' as const };
  type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

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
    memoryOpen = page.url.searchParams.get('memory') === '1';
    unsubscribeCatalog = agentModelCatalogStore.subscribe((state) => {
      rebuildSettingsView(state);
    });
    void loadSettings();
  });

  onDestroy(() => {
    unsubscribeCatalog?.();
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

  function openSetupChat(kind: SetupPromptKind): void {
    const params = new URLSearchParams({
      new: 'local',
      draft: setupPrompts[kind]
    });
    void goto(href(`/chats?${params.toString()}`));
  }
</script>

<SettingsView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {sessionBaselineEpoch}
  {view}
  errorMessage={error?.message ?? null}
  saveError={saveError?.message ?? null}
  {saving}
  onSessionChange={updateSession}
  onSavePreferences={savePreferences}
  onOpenPmaMemory={() => (memoryOpen = true)}
  onOpenSetupChat={openSetupChat}
/>
<MemoryRail open={memoryOpen} scope={hubScope} onClose={() => (memoryOpen = false)} />
