<script lang="ts">
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import type { ContextspaceViewModel, ContextspaceDocKind } from '$lib/viewModels/contextspace';

  let {
    state: viewState,
    vm = null,
    errorMessage = null,
    onSaveDoc
  }: {
    state: 'loading' | 'error' | 'ready';
    vm?: ContextspaceViewModel | null;
    errorMessage?: string | null;
    onSaveDoc?: (docId: string, content: string) => Promise<boolean> | boolean;
  } = $props();

  let activeDocId = $state<ContextspaceDocKind>('active_context');
  let copyState = $state('Copy');

  const activeDoc = $derived(vm?.docs.find((doc) => doc.id === activeDocId) ?? vm?.docs[0] ?? null);

  async function copyActiveDoc(): Promise<void> {
    if (!activeDoc) return;
    try {
      await navigator.clipboard.writeText(activeDoc.content);
      copyState = 'Copied';
      window.setTimeout(() => (copyState = 'Copy'), 1600);
    } catch {
      copyState = 'Copy failed';
      window.setTimeout(() => (copyState = 'Copy'), 1800);
    }
  }
</script>

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading contextspace docs...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load contextspace. {errorMessage}</div>
  </section>
{:else if vm && activeDoc}
  <section class="page-stack contextspace-page">
    <PageHero
      title={vm.title}
      subtitle={`${vm.description} ${vm.presentCount} of ${vm.docs.length} standard docs have content.`}
    >
      {#snippet actions()}
        <a class="hero-action" href={href(vm.openWorkspaceHref)}>{vm.openWorkspaceLabel}</a>
        <a class="hero-action" href={href(vm.askPmaHref)}>Ask PMA to update</a>
      {/snippet}
    </PageHero>

    <div class="contextspace-layout">
      <aside class="page-panel contextspace-doc-list" aria-label="Scoped workspace contextspace documents">
        <h2>Documents</h2>
        <div class="doc-tab-list">
          {#each vm.docs as doc}
            <button
              class:active={doc.id === activeDoc.id}
              class:missing={doc.isMissing}
              type="button"
              onclick={() => (activeDocId = doc.id)}
            >
              <span>{doc.label}</span>
              <small>{doc.filename}{doc.isMissing ? ' · missing' : ''}</small>
            </button>
          {/each}
        </div>
      </aside>

      <article class="page-panel contextspace-reader">
        <div class="panel-heading-row">
          <div>
            <h2>{activeDoc.filename}</h2>
            <p>{activeDoc.isMissing ? 'No content has been written yet.' : 'Readable markdown preview.'}</p>
          </div>
          <button class="secondary-button" type="button" onclick={copyActiveDoc} disabled={activeDoc.isMissing}>
            {copyState}
          </button>
        </div>

        <EditableMarkdown
          docId={activeDoc.id}
          content={activeDoc.content}
          html={activeDoc.html}
          isMissing={activeDoc.isMissing}
          editable={!vm.isUnknown}
          emptyTitle={`${activeDoc.label} has no content`}
          emptyMessage={`Ask PMA to refresh this ${vm.workspaceKind} memory before starting work that depends on shared context.`}
          emptyActionHref={href(vm.askPmaHref)}
          emptyActionLabel="Ask PMA to update"
          onSave={onSaveDoc}
        />
      </article>
    </div>
  </section>
{/if}
