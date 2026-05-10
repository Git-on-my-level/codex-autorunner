<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

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
  let lastSeen = $state<string | null>(null);
  let progress = $state(0);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let raf: number | null = null;
  let startedAt = 0;
  let toastEl: HTMLDivElement | null = $state(null);
  let portalParent: HTMLElement | null = null;

  function clearTimer(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (raf !== null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
  }

  function dismiss(): void {
    visibleMessage = null;
    clearTimer();
  }

  function startCountdown(): void {
    clearTimer();
    if (!visibleMessage || timeoutMs <= 0) return;
    startedAt = performance.now();
    progress = 0;
    const tick = () => {
      const elapsed = performance.now() - startedAt;
      progress = Math.min(1, elapsed / timeoutMs);
      if (progress < 1 && visibleMessage) {
        raf = requestAnimationFrame(tick);
      }
    };
    raf = requestAnimationFrame(tick);
    timer = setTimeout(() => {
      visibleMessage = null;
      timer = null;
    }, timeoutMs);
  }

  function ensureStack(): HTMLElement {
    if (typeof document === 'undefined') return null as unknown as HTMLElement;
    let stack = document.querySelector<HTMLElement>('.toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'toast-stack';
      stack.setAttribute('aria-live', 'polite');
      stack.setAttribute('aria-atomic', 'false');
      document.body.appendChild(stack);
    }
    return stack;
  }

  $effect(() => {
    const next = message;
    if (next !== lastSeen) {
      lastSeen = next;
      visibleMessage = next;
      startCountdown();
    }
  });

  $effect(() => {
    if (!toastEl) return;
    const stack = ensureStack();
    if (!stack) return;
    portalParent = stack;
    stack.appendChild(toastEl);
    return () => {
      const parent = portalParent;
      if (parent && toastEl && toastEl.parentNode === parent) {
        parent.removeChild(toastEl);
      }
    };
  });

  onMount(() => ensureStack());
  onDestroy(clearTimer);
</script>

{#if visibleMessage}
  <div bind:this={toastEl} class={`auto-dismiss-notice ${tone}`} role="status">
    <span class="auto-dismiss-notice__msg">{visibleMessage}</span>
    <span class="auto-dismiss-notice__dot" aria-hidden="true"></span>
    <button type="button" aria-label="Dismiss notice" onclick={dismiss}>×</button>
    <span
      class="auto-dismiss-notice__bar"
      aria-hidden="true"
      style:transform={`scaleX(${1 - progress})`}
    ></span>
  </div>
{/if}
