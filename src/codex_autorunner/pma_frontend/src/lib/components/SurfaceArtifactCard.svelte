<script lang="ts">
  import type { SurfaceArtifact } from '$lib/viewModels/domain';
  import { artifactCardView } from '$lib/viewModels/pmaChat';

  let { artifact }: { artifact: SurfaceArtifact } = $props();

  const view = $derived(artifactCardView(artifact));
  const rawJson = $derived(JSON.stringify(artifact.raw, null, 2));
</script>

<article class={`artifact-card ${view.tone}`}>
  <span class="artifact-type">{view.label}</span>
  <strong>{artifact.title}</strong>
  <p>{artifact.summary ?? artifact.url ?? 'Surfaced PMA artifact.'}</p>
  {#if view.preview === 'image' && artifact.url}
    <img class="artifact-image-preview" src={artifact.url} alt={artifact.title} loading="lazy" />
  {:else if view.preview === 'link' && artifact.url}
    <div class="artifact-url-preview">{artifact.url}</div>
  {:else if view.preview === 'file'}
    <div class="artifact-file-preview">{artifact.url ?? artifact.title}</div>
  {/if}
  <div class="artifact-actions">
    {#if artifact.url && view.primaryAction}
      <a href={artifact.url}>{view.primaryAction}</a>
    {/if}
    <details>
      <summary>{view.detailLabel}</summary>
      <dl>
        <div>
          <dt>Kind</dt>
          <dd>{artifact.kind}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{artifact.createdAt ?? 'Unknown'}</dd>
        </div>
        {#if artifact.url}
          <div>
            <dt>URL</dt>
            <dd>{artifact.url}</dd>
          </div>
        {/if}
      </dl>
      <pre>{rawJson}</pre>
    </details>
  </div>
</article>
