<script lang="ts">
  import { onDestroy } from 'svelte';
  import { confirmPendingStore as _pendingStore, type ConfirmPending as Pending } from './confirmDialog';

  let dialog: HTMLDialogElement | null = $state(null);
  let pending = $state<Pending | null>(null);
  let typedValue = $state('');
  let typeError = $state<string | null>(null);
  let confirmBtn = $state<HTMLButtonElement | null>(null);

  const unsubscribe = _pendingStore.subscribe((next) => {
    pending = next;
    typedValue = '';
    typeError = null;
    if (next && dialog && !dialog.open) {
      try {
        dialog.showModal();
        queueMicrotask(() => {
          if (pending?.requireType) {
            (dialog?.querySelector('input') as HTMLInputElement | null)?.focus();
          } else {
            confirmBtn?.focus();
          }
        });
      } catch {
        /* ignore */
      }
    } else if (!next && dialog?.open) {
      dialog.close();
    }
  });

  onDestroy(unsubscribe);

  function settle(value: string | boolean): void {
    const p = pending;
    if (!p) return;
    pending = null;
    _pendingStore.set(null);
    p.resolve(value);
  }

  function onConfirm(): void {
    if (!pending) return;
    if (pending.requireType) {
      if (typedValue.trim() !== pending.requireType) {
        typeError = `Type "${pending.requireType}" exactly to confirm.`;
        return;
      }
      settle(typedValue.trim());
      return;
    }
    settle(true);
  }

  function onCancel(): void {
    settle(false);
  }

  function onBackdropClick(event: MouseEvent): void {
    if (event.target === dialog) settle(false);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !pending?.requireType) {
      event.preventDefault();
      onConfirm();
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="confirm-dialog"
  class:danger={pending?.danger}
  onclose={onCancel}
  onclick={onBackdropClick}
  onkeydown={onKeydown}
>
  {#if pending}
    <form method="dialog" onsubmit={(e) => { e.preventDefault(); onConfirm(); }}>
      {#if pending.title}
        <h2 class="confirm-dialog__title">{pending.title}</h2>
      {/if}
      <p class="confirm-dialog__message">{pending.message}</p>
      {#if pending.requireType}
        <label class="confirm-dialog__type">
          <span>Type <code>{pending.requireType}</code> to confirm:</span>
          <input
            type="text"
            bind:value={typedValue}
            spellcheck="false"
            autocomplete="off"
            autocapitalize="off"
          />
          {#if typeError}<small class="confirm-dialog__error">{typeError}</small>{/if}
        </label>
      {/if}
      <div class="confirm-dialog__actions">
        <button type="button" class="ghost-button" onclick={onCancel}>
          {pending.cancelText ?? 'Cancel'}
        </button>
        <button
          bind:this={confirmBtn}
          type="submit"
          class={pending.danger ? 'primary-button danger' : 'primary-button'}
        >
          {pending.confirmText ?? 'Confirm'}
        </button>
      </div>
    </form>
  {/if}
</dialog>

<style>
  .confirm-dialog {
    border: 1px solid var(--color-accent);
    border-radius: var(--radius-2);
    background: var(--color-surface);
    color: var(--color-ink);
    padding: 0;
    max-width: min(420px, calc(100vw - var(--space-6)));
    box-shadow: 0 0 0 1px var(--color-accent-soft),
      0 24px 60px -20px rgb(0 0 0 / 0.55);
    font-family: var(--font-mono);
  }

  .confirm-dialog.danger {
    border-color: var(--color-danger);
    box-shadow: 0 0 0 1px var(--color-danger-soft),
      0 24px 60px -20px rgb(0 0 0 / 0.55);
  }

  .confirm-dialog::backdrop {
    background: rgb(0 0 0 / 0.55);
    backdrop-filter: blur(2px);
  }

  .confirm-dialog form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5) var(--space-5) var(--space-4);
  }

  .confirm-dialog__title {
    margin: 0;
    font-size: var(--font-size-3);
    font-weight: 600;
    color: var(--color-accent);
  }
  .confirm-dialog.danger .confirm-dialog__title {
    color: var(--color-danger);
  }

  .confirm-dialog__message {
    margin: 0;
    font-size: var(--font-size-2);
    line-height: 1.45;
    color: var(--color-ink-soft);
    white-space: pre-wrap;
  }

  .confirm-dialog__type {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    font-size: var(--font-size-1);
    color: var(--color-ink-muted);
  }
  .confirm-dialog__type code {
    color: var(--color-accent);
    background: var(--color-accent-soft);
    padding: 1px 4px;
    border-radius: 2px;
  }
  .confirm-dialog__type input {
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    font-family: var(--font-mono);
    font-size: var(--font-size-2);
  }
  .confirm-dialog__type input:focus-visible {
    outline: none;
    border-color: var(--color-accent);
    box-shadow: 0 0 0 2px var(--color-accent-soft);
  }
  .confirm-dialog__error {
    color: var(--color-danger);
    font-size: var(--font-size-0);
  }

  .confirm-dialog__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px solid var(--color-border-subtle);
    margin-top: var(--space-2);
  }

  :global(.confirm-dialog .primary-button.danger) {
    background: var(--color-danger);
    border-color: var(--color-danger);
    color: var(--color-bg);
    box-shadow: 0 0 0 3px var(--color-danger-soft);
  }
  :global(.confirm-dialog .primary-button.danger:hover:not(:disabled)) {
    background: var(--color-danger);
    filter: brightness(1.1);
  }
</style>
