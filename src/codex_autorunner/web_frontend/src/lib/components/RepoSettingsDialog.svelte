<script lang="ts" module>
  export type RepoSettingsTarget = {
    id: string;
    label: string;
    worktreeSetupCommands: string[];
  };
</script>

<script lang="ts">
  import { submitSetWorktreeSetup, type ActionNotice } from '$lib/actions/repoWorktreeActions';

  let {
    open = $bindable(false),
    target = null,
    onResult
  }: {
    open?: boolean;
    target?: RepoSettingsTarget | null;
    onResult: (notice: ActionNotice) => void | Promise<void>;
  } = $props();

  let dialog: HTMLDialogElement | null = $state(null);
  let commandsText = $state('');
  let submitting = $state(false);
  let inlineError = $state<string | null>(null);

  $effect(() => {
    if (!dialog) return;
    if (open && target && !dialog.open) {
      commandsText = target.worktreeSetupCommands.join('\n');
      submitting = false;
      inlineError = null;
      try {
        dialog.showModal();
        queueMicrotask(() => {
          (dialog?.querySelector('textarea') as HTMLTextAreaElement | null)?.focus();
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
    const commands = commandsText.split(/\r?\n/);
    const notice = await submitSetWorktreeSetup({
      repoId: target.id,
      repoLabel: target.label,
      commands
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
      <h2 class="form-dialog__title">Settings · {target.label}</h2>

      <section class="form-dialog__section">
        <h3 class="form-dialog__section-title">Worktree setup</h3>
        <label class="form-dialog__field">
          <span>Commands to run on new worktree creation</span>
          <textarea
            bind:value={commandsText}
            rows="6"
            placeholder={'npm install\nbun install\n# one command per line'}
            spellcheck="false"
            autocapitalize="off"
          ></textarea>
          <small>Each line is run as a separate command from the worktree root after checkout. Lines are trimmed; blanks are ignored.</small>
        </label>
      </section>

      {#if inlineError}
        <p class="form-dialog__error" role="alert">{inlineError}</p>
      {/if}

      <div class="form-dialog__actions">
        <button type="button" class="ghost-button" onclick={close} disabled={submitting}>
          Cancel
        </button>
        <button type="submit" class="primary-button" disabled={submitting}>
          {submitting ? 'Saving…' : 'Save settings'}
        </button>
      </div>
    </form>
  {/if}
</dialog>

<style>
  .form-dialog__section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .form-dialog__section-title {
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--color-fg-muted, #6b6657);
    margin: 0;
  }
  textarea {
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 0.9rem;
    resize: vertical;
    min-height: 6em;
  }
</style>
