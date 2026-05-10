<script lang="ts">
  import { submitCreateWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';

  let {
    open = $bindable(false),
    target = null,
    onResult
  }: {
    open?: boolean;
    target?: { id: string; label: string } | null;
    onResult: (notice: ActionNotice) => void | Promise<void>;
  } = $props();

  let dialog: HTMLDialogElement | null = $state(null);
  let branch = $state('');
  let submitting = $state(false);
  let inlineError = $state<string | null>(null);

  $effect(() => {
    if (!dialog) return;
    if (open && target && !dialog.open) {
      branch = '';
      submitting = false;
      inlineError = null;
      try {
        dialog.showModal();
        queueMicrotask(() => {
          (dialog?.querySelector('input') as HTMLInputElement | null)?.focus();
        });
      } catch {
        /* ignore */
      }
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  function close(): void {
    open = false;
  }

  async function onSubmit(event: Event): Promise<void> {
    event.preventDefault();
    if (!target || submitting) return;
    inlineError = null;
    submitting = true;
    const notice = await submitCreateWorktree({
      baseRepoId: target.id,
      baseRepoLabel: target.label,
      branch
    });
    submitting = false;
    if (notice.tone === 'success') {
      close();
      await onResult(notice);
    } else {
      inlineError = notice.message;
    }
  }

  function onBackdropClick(event: MouseEvent): void {
    if (event.target === dialog) close();
  }
</script>

<dialog
  bind:this={dialog}
  class="form-dialog"
  onclose={close}
  onclick={onBackdropClick}
>
  {#if target}
    <form onsubmit={onSubmit}>
      <h2 class="form-dialog__title">New worktree on {target.label}</h2>
      <p class="form-dialog__hint">
        Created from a fresh <code>origin/main</code> (after <code>git fetch --prune origin</code>),
        not from local <code>main</code>, to avoid stale starting points.
      </p>

      <label class="form-dialog__field">
        <span>Branch name</span>
        <input
          type="text"
          bind:value={branch}
          placeholder="feat/short-description"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
          required
        />
      </label>

      {#if inlineError}
        <p class="form-dialog__error" role="alert">{inlineError}</p>
      {/if}

      <div class="form-dialog__actions">
        <button type="button" class="ghost-button" onclick={close} disabled={submitting}>
          Cancel
        </button>
        <button type="submit" class="primary-button" disabled={submitting}>
          {submitting ? 'Creating…' : 'Create worktree'}
        </button>
      </div>
    </form>
  {/if}
</dialog>
