<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import MemoryRail from '$lib/components/MemoryRail.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import SettingsView from '$lib/components/SettingsView.svelte';
  import {
    buildSessionUpdatePayload,
    buildSettingsViewModel,
    type SettingsSessionState,
    type SettingsViewModel
  } from '$lib/viewModels/settings';
  import { agentCanListModels, agentId } from '$lib/viewModels/modelPickers';

  let view = $state<SettingsViewModel | null>(null);
  let sessionBaselineEpoch = $state(0);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let saveError = $state<ApiError | null>(null);
  let saving = $state(false);
  let memoryOpen = $state(false);
  const hubScope = { kind: 'hub' as const };
  type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

  const setupPrompts: Record<SetupPromptKind, string> = {
    telegram:
      'Walk me through setting up Telegram for this PMA Hub. Please inspect the current hub config, ask for any missing bot/token/chat details, and make the smallest safe config changes. Do not start or restart services without asking me first.',
    discord:
      'Walk me through setting up Discord for this PMA Hub. Please inspect the current hub config, cover slash commands, PMA mode, allowlists, and voice/media options, and make the smallest safe config changes. Do not start or restart services without asking me first.',
    notifications:
      'Help me configure PMA Hub notifications for run_finished, run_error, and tui_idle. Please inspect the current notification config, explain the available Discord and Telegram delivery options, and make the smallest safe config changes.',
    github:
      'Help me configure GitHub automation for this PMA Hub. Please inspect the current config and explain webhook, PR binding, and review workflow options before making changes.',
    voice:
      'Walk me through enabling voice transcription for this PMA Hub. Please inspect the current voice config, confirm whether local Whisper or a remote provider is appropriate, check any required runtime extras or API key env vars, and make the smallest safe config changes. Do not start or restart services without asking me first.'
  };

  onMount(() => {
    memoryOpen = page.url.searchParams.get('memory') === '1';
    void loadSettings();
  });

  async function loadSettings(): Promise<void> {
    loading = true;
    error = null;
    saveError = null;
    const [session, agents, voice] = await Promise.all([
      pmaApi.settings.getSession(),
      pmaApi.pma.listAgents(),
      pmaApi.voice.getConfig()
    ]);

    if (!session.ok && !agents.ok && !voice.ok) {
      error = session.error;
      loading = false;
      return;
    }

    const agentRows = agents.ok ? agents.data : [];
    const modelCatalogs: Record<string, JsonRecord[] | null> = {};
    await Promise.all(
      agentRows.map(async (agent) => {
        if (!agentCanListModels(agent)) return;
        const id = agentId(agent);
        const result = await pmaApi.pma.listAgentModels(id);
        modelCatalogs[id] = result.ok ? result.data : null;
      })
    );

    view = buildSettingsViewModel({
      session: session.ok ? session.data : null,
      agents: agentRows,
      modelCatalogs,
      voiceConfig: voice.ok ? voice.data : null
    });
    sessionBaselineEpoch += 1;
    loading = false;
  }

  function updateSession(session: SettingsSessionState): void {
    if (!view) return;
    view = { ...view, session };
  }

  async function savePreferences(): Promise<void> {
    if (!view || saving) return;
    saving = true;
    saveError = null;
    const result = await pmaApi.settings.updateSession(buildSessionUpdatePayload(view.session));
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
