<script lang="ts">
  import { onMount } from 'svelte';
  import MarkdownDocViewer from '$lib/components/MarkdownDocViewer.svelte';
  import type { ContextspaceViewModel } from '$lib/viewModels/contextspace';

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

  let activeDocId = $state<string>('active_context');

  onMount(() => {
    selectDocFromLocation({ replace: true });
    window.addEventListener('hashchange', selectDocFromLocation);
    window.addEventListener('popstate', selectDocFromLocation);
    return () => {
      window.removeEventListener('hashchange', selectDocFromLocation);
      window.removeEventListener('popstate', selectDocFromLocation);
    };
  });

  $effect(() => {
    if (!vm) return;
    selectDocFromLocation();
    if (!vm.docs.some((doc) => doc.id === activeDocId)) {
      activeDocId = vm.docs[0]?.id ?? 'active_context';
    }
  });

  function docHref(doc: { id: string }): string {
    return `#${encodeURIComponent(doc.id)}`;
  }

  function selectDoc(docId: string): void {
    activeDocId = docId;
    if (typeof window === 'undefined') return;
    const nextUrl = new URL(window.location.href);
    nextUrl.hash = encodeURIComponent(docId);
    window.history.pushState(null, '', nextUrl);
  }

  function selectDocFromLocation(_event?: Event | { replace?: boolean }): void {
    if (typeof window === 'undefined' || !vm) return;
    const hashDoc = normalizeDocToken(window.location.hash);
    const queryDoc = normalizeDocToken(new URL(window.location.href).searchParams.get('doc'));
    const target = [hashDoc, queryDoc].find((candidate) => candidate && vm.docs.some((doc) => doc.id === candidate));
    if (target) activeDocId = target;
  }

  function normalizeDocToken(value: string | null): string | null {
    if (!value) return null;
    const decoded = decodeURIComponent(value.replace(/^#/, '').trim()).toLowerCase();
    const withoutExtension = decoded.replace(/\.md$/, '');
    if (withoutExtension === 'active' || withoutExtension === 'active-context') return 'active_context';
    if (withoutExtension === 'active_context' || withoutExtension === 'spec' || withoutExtension === 'decisions') {
      return withoutExtension;
    }
    return null;
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
{:else if vm}
  <MarkdownDocViewer
    title={vm.title}
    description={`${vm.description} ${vm.presentCount} of ${vm.docs.length} standard docs have content.`}
    docs={vm.docs}
    presentCount={vm.presentCount}
    ariaLabel="Contextspace documents"
    {activeDocId}
    emptyMessage="Click to add durable context for future PMA and ticket-flow work."
    editableDoc={() => !vm?.isUnknown}
    {docHref}
    onSelectDoc={selectDoc}
    {onSaveDoc}
  />
{/if}
