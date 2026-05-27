<script lang="ts">
  import type { SettingsStatusItem, SettingsVoiceStatus } from '$lib/viewModels/settings';

  export type SetupPromptKind = 'telegram' | 'discord' | 'notifications' | 'github' | 'voice';

  let {
    voice,
    onOpenSetupChat
  }: {
    voice: SettingsVoiceStatus;
    onOpenSetupChat?: (kind: SetupPromptKind) => void;
  } = $props();

  const SETUP_CARDS: { kind: SetupPromptKind; title: string; description: string }[] = [
    { kind: 'telegram', title: 'Telegram', description: 'Interactive mobile chat, topics, allowlists' },
    { kind: 'discord', title: 'Discord', description: 'Slash commands, PMA mode, voice, channels' },
    { kind: 'notifications', title: 'Notifications', description: 'Run finished, run error, idle alerts' },
    { kind: 'github', title: 'GitHub automation', description: 'Webhooks, PR bindings, review workflows' }
  ];
</script>

<section class="settings-section">
  <h2 class="settings-section-title">Setup with PMA</h2>
  <div class="settings-action-grid">
    {#each SETUP_CARDS as card}
      <button type="button" class="setup-action" onclick={() => onOpenSetupChat?.(card.kind)}>
        <strong>{card.title}</strong>
        <span>{card.description}</span>
      </button>
    {/each}
  </div>
</section>

<section class="settings-section">
  <div class="settings-section-head">
    <h2 class="settings-section-title">Voice transcription</h2>
    <div class="settings-section-actions">
      {#if voice.enabled}
        <span class="status-pill done">enabled</span>
      {:else}
        <span class="status-pill waiting">disabled</span>
        <button type="button" class="ghost-button" onclick={() => onOpenSetupChat?.('voice')}>
          Enable with PMA
        </button>
      {/if}
    </div>
  </div>
  <dl class="settings-status-list">
    {#each voice.rows as item (item.label)}
      <div class={item.tone}>
        <dt>{item.label}</dt>
        <dd>{item.value}</dd>
      </div>
    {/each}
  </dl>
  {#if voice.hint}
    <p class="voice-hint">{voice.hint}</p>
  {/if}
  {#if !voice.enabled && voice.apiKeyEnv}
    <p class="voice-hint voice-hint-cmd">
      Set the env var, then restart the hub:
      <code>export {voice.apiKeyEnv}=…</code>
    </p>
  {/if}
</section>

<style>
  .settings-action-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
    gap: var(--space-3);
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
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-0);
  }
</style>
