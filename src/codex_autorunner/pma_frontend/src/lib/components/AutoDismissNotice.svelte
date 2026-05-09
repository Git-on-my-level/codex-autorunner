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
  let timer: ReturnType<typeof setTimeout> | null = null;
  let toastEl: HTMLDivElement | null = $state(null);
  let portalParent: HTMLElement | null = null;

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

  // Portal: move our rendered toast into a single shared top-right stack so
  // multiple notices anywhere in the tree stack vertically and float over
  // page content rather than getting trapped inside their parent's flow.
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
    visibleMessage = message;
    dismissAfterDelay();
  });

  // Re-parent the DOM node into the shared stack each time it is created.
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
    <span>{visibleMessage}</span>
    <button type="button" aria-label="Dismiss notice" onclick={() => { visibleMessage = null; clearTimer(); }}>×</button>
  </div>
{/if}
