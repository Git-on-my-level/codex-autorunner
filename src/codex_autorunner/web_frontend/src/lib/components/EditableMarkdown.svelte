<script lang="ts">
  import { tick } from 'svelte';

  let {
    docId,
    content,
    html,
    isMissing = false,
    emptyTitle,
    emptyMessage,
    emptyActionHref = null,
    emptyActionLabel = null,
    editable = true,
    onSave
  }: {
    docId: string;
    content: string;
    html: string;
    isMissing?: boolean;
    emptyTitle: string;
    emptyMessage: string;
    emptyActionHref?: string | null;
    emptyActionLabel?: string | null;
    editable?: boolean;
    onSave?: (docId: string, content: string) => Promise<boolean> | boolean;
  } = $props();

  let draft = $state('');
  let editing = $state(false);
  let saving = $state(false);
  let saveState = $state('');
  let lastDocId = $state('');
  let textarea = $state<HTMLTextAreaElement | null>(null);

  $effect(() => {
    if (docId !== lastDocId) {
      lastDocId = docId;
      draft = content;
      editing = false;
      saving = false;
      saveState = '';
      return;
    }
    if (!editing && draft !== content) {
      draft = content;
    }
  });

  async function startEditing(): Promise<void> {
    if (!editable || saving) return;
    draft = content;
    editing = true;
    saveState = '';
    await tick();
    resizeEditor();
    textarea?.focus();
  }

  async function commitEdit(): Promise<void> {
    if (!editing || saving) return;
    const nextContent = draft;
    const targetDocId = docId;
    if (nextContent === content) {
      editing = false;
      saveState = '';
      return;
    }
    saving = true;
    saveState = '';
    const ok = onSave ? await onSave(targetDocId, nextContent) : false;
    saving = false;
    if (ok) {
      editing = false;
      saveState = 'Saved';
      window.setTimeout(() => {
        if (saveState === 'Saved') saveState = '';
      }, 1400);
      return;
    }
    saveState = 'Save failed';
  }

  function cancelEdit(): void {
    draft = content;
    editing = false;
    saving = false;
    saveState = '';
  }

  function handleEditorKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void commitEdit();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelEdit();
    }
  }

  function handlePreviewKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      void startEditing();
    }
  }

  function resizeEditor(): void {
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight}px`;
  }
</script>

{#if editing}
  <div class="markdown-editor-shell">
    <textarea
      bind:this={textarea}
      class="markdown-editor"
      bind:value={draft}
      spellcheck="false"
      aria-label={docId}
      disabled={saving}
      onblur={() => void commitEdit()}
      oninput={resizeEditor}
      onkeydown={handleEditorKeydown}
    ></textarea>
    {#if saveState || saving}
      <span class="markdown-save-state">{saving ? 'Saving...' : saveState}</span>
    {/if}
  </div>
{:else if isMissing}
  {#if editable}
    <div
      class="state-panel contextspace-empty markdown-edit-target markdown-editable"
      role="button"
      tabindex="0"
      onclick={() => void startEditing()}
      onkeydown={handlePreviewKeydown}
    >
      <strong>{emptyTitle}</strong>
      <p>{emptyMessage}</p>
      {#if emptyActionHref && emptyActionLabel}
        <a class="inline-link" href={emptyActionHref} onclick={(event) => event.stopPropagation()}>{emptyActionLabel}</a>
      {/if}
      {#if saveState}
        <span class="markdown-save-state">{saveState}</span>
      {/if}
    </div>
  {:else}
    <div class="state-panel contextspace-empty markdown-edit-target">
      <strong>{emptyTitle}</strong>
      <p>{emptyMessage}</p>
      {#if emptyActionHref && emptyActionLabel}
        <a class="inline-link" href={emptyActionHref}>{emptyActionLabel}</a>
      {/if}
      {#if saveState}
        <span class="markdown-save-state">{saveState}</span>
      {/if}
    </div>
  {/if}
{:else}
  {#if editable}
    <div
      class="markdown-body markdown-edit-target markdown-editable"
      role="button"
      tabindex="0"
      onclick={() => void startEditing()}
      onkeydown={handlePreviewKeydown}
    >
      {@html html}
      {#if saveState}
        <span class="markdown-save-state">{saveState}</span>
      {/if}
    </div>
  {:else}
    <div class="markdown-body markdown-edit-target">
      {@html html}
      {#if saveState}
        <span class="markdown-save-state">{saveState}</span>
      {/if}
    </div>
  {/if}
{/if}
