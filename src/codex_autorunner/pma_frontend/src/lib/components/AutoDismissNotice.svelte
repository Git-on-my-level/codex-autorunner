<script lang="ts">
  import { onDestroy } from 'svelte';

  let {
    message = null,
    tone = 'neutral',
    timeoutMs = 3500
  }: {
    message?: string | null;
    tone?: 'neutral' | 'success' | 'warning' | 'danger';
    timeoutMs?: number;
  } = $props();

  let visibleMessage = $state<string | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;

  function clearTimer(): void {
    if (!timer) return;
    clearTimeout(timer);
    timer = null;
  }

  function dismissAfterDelay(): void {
    clearTimer();
    if (!visibleMessage || timeoutMs <= 0) return;
    timer = setTimeout(() => {
      visibleMessage = null;
      timer = null;
    }, timeoutMs);
  }

  $effect(() => {
    visibleMessage = message;
    dismissAfterDelay();
  });

  onDestroy(clearTimer);
</script>

{#if visibleMessage}
  <div class={`auto-dismiss-notice ${tone}`} role="status" aria-live="polite">
    <span>{visibleMessage}</span>
    <button type="button" aria-label="Dismiss notice" onclick={() => { visibleMessage = null; clearTimer(); }}>×</button>
  </div>
{/if}
