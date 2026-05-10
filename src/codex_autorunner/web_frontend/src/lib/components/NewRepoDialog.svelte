<script lang="ts">
  import { submitCreateRepo, type ActionNotice } from '$lib/actions/repoWorktreeActions';

  let {
    open = $bindable(false),
    onResult
  }: {
    open?: boolean;
    onResult: (notice: ActionNotice) => void | Promise<void>;
  } = $props();

  let dialog: HTMLDialogElement | null = $state(null);
  let mode = $state<'create' | 'clone'>('create');
  let repoId = $state('');
  let gitUrl = $state('');
  let submitting = $state(false);
  let inlineError = $state<string | null>(null);

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      reset();
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

  function reset(): void {
    mode = 'create';
    repoId = '';
    gitUrl = '';
    submitting = false;
    inlineError = null;
  }

  function close(): void {
    open = false;
  }

  async function onSubmit(event: Event): Promise<void> {
    event.preventDefault();
    if (submitting) return;
    inlineError = null;
    submitting = true;
    const notice = await submitCreateRepo(
      mode === 'create' ? { repoId } : { gitUrl, repoId: repoId || undefined }
    );
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
  <form onsubmit={onSubmit}>
    <h2 class="form-dialog__title">New repo</h2>

    <div class="form-dialog__tabs" role="tablist" aria-label="Repo source">
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'create'}
        class:active={mode === 'create'}
        onclick={() => (mode = 'create')}
      >
        Create local
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'clone'}
        class:active={mode === 'clone'}
        onclick={() => (mode = 'clone')}
      >
        Clone from URL
      </button>
    </div>

    {#if mode === 'create'}
      <label class="form-dialog__field">
        <span>Repo name</span>
        <input
          type="text"
          bind:value={repoId}
          placeholder="my-new-repo"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
          required
        />
        <small>Initializes a new local git repo with this id.</small>
      </label>
    {:else}
      <label class="form-dialog__field">
        <span>Git URL</span>
        <input
          type="url"
          bind:value={gitUrl}
          placeholder="https://github.com/owner/repo.git"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
          required
        />
        <small>HTTPS, SSH, or any URL <code>git clone</code> accepts.</small>
      </label>
      <label class="form-dialog__field">
        <span>Repo id <em>(optional)</em></span>
        <input
          type="text"
          bind:value={repoId}
          placeholder="leave blank to derive from URL"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
        />
      </label>
    {/if}

    {#if inlineError}
      <p class="form-dialog__error" role="alert">{inlineError}</p>
    {/if}

    <div class="form-dialog__actions">
      <button type="button" class="ghost-button" onclick={close} disabled={submitting}>
        Cancel
      </button>
      <button type="submit" class="primary-button" disabled={submitting}>
        {submitting ? (mode === 'create' ? 'Creating…' : 'Cloning…') : (mode === 'create' ? 'Create repo' : 'Clone repo')}
      </button>
    </div>
  </form>
</dialog>
