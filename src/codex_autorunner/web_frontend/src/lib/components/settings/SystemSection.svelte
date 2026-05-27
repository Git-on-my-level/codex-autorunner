<script lang="ts">
  import type { SettingsStatusItem } from '$lib/viewModels/settings';
  import type { SystemUpdateStatus, SystemUpdateTargetOption } from '$lib/api/client';
  import AutoDismissNotice from '../AutoDismissNotice.svelte';
  import DropdownSelect from '../DropdownSelect.svelte';
  import type { DropdownSelectOption } from '../DropdownSelect';

  let {
    degradedHub,
    updateTargets,
    selectedUpdateTarget,
    updateStatus,
    updateLoading,
    updateStarting,
    updateMessage,
    updateError,
    pendingUpdateConfirmation,
    onSelectUpdateTarget,
    onRefreshUpdateStatus,
    onStartUpdate,
    onConfirmUpdate
  }: {
    degradedHub: SettingsStatusItem[];
    updateTargets: SystemUpdateTargetOption[];
    selectedUpdateTarget: string;
    updateStatus: SystemUpdateStatus | null;
    updateLoading: boolean;
    updateStarting: boolean;
    updateMessage: string | null;
    updateError: string | null;
    pendingUpdateConfirmation: boolean;
    onSelectUpdateTarget?: (target: string) => void;
    onRefreshUpdateStatus?: () => void;
    onStartUpdate?: () => void;
    onConfirmUpdate?: () => void;
  } = $props();

  const updateTargetOptions = $derived<DropdownSelectOption[]>(
    updateTargets.map((target) => ({
      value: target.value,
      label: target.description ? `${target.label} — ${target.description}` : target.label
    }))
  );
  const activeUpdateTarget = $derived(
    updateTargets.find((target) => target.value === selectedUpdateTarget) ?? null
  );

  function updateStatusRows(status: SystemUpdateStatus | null): SettingsStatusItem[] {
    if (!status) return [{ label: 'Status', value: 'Not loaded', tone: 'muted' }];
    const rows: SettingsStatusItem[] = [
      {
        label: 'Status',
        value: status.status,
        tone: status.status === 'error' ? 'warning' : status.status === 'unknown' ? 'muted' : 'ok'
      }
    ];
    if (status.updateTarget) rows.push({ label: 'Target', value: status.updateTarget, tone: 'muted' });
    if (status.phase) rows.push({ label: 'Phase', value: status.phase, tone: 'muted' });
    return rows;
  }
</script>

{#if degradedHub.length > 0}
  <div class="state-panel error" role="status">
    {#each degradedHub as item (item.label)}
      <div><strong>{item.label}:</strong> {item.value}</div>
    {/each}
  </div>
{/if}

<section class="settings-section">
  <div class="settings-section-head">
    <h2 class="settings-section-title">System update</h2>
    <div class="settings-section-actions">
      <button
        type="button"
        class="ghost-button"
        disabled={updateLoading}
        onclick={() => onRefreshUpdateStatus?.()}
      >
        {updateLoading ? 'Refreshing…' : 'Refresh status'}
      </button>
    </div>
  </div>
  <div class="update-control-row">
    {#if updateTargetOptions.length > 0}
      <DropdownSelect
        value={selectedUpdateTarget}
        options={updateTargetOptions}
        labelText="Update target"
        ariaLabel="Update target"
        onchange={(value) => onSelectUpdateTarget?.(value)}
      />
    {:else}
      <div class="state-panel empty-state compact-empty">
        <strong>Update targets unavailable</strong>
        <p>Could not load update target options from the hub.</p>
      </div>
    {/if}
    <div class="update-action-group">
      <button
        type="button"
        class="ghost-button dirty"
        disabled={updateStarting || updateTargetOptions.length === 0}
        onclick={() => onStartUpdate?.()}
      >
        {updateStarting ? 'Starting…' : 'Start update'}
      </button>
      {#if pendingUpdateConfirmation}
        <button
          type="button"
          class="ghost-button dirty"
          disabled={updateStarting}
          onclick={() => onConfirmUpdate?.()}
        >
          Confirm restart
        </button>
      {/if}
    </div>
  </div>
  {#if activeUpdateTarget?.restartNotice}
    <p class="update-hint">{activeUpdateTarget.restartNotice}</p>
  {/if}
  <dl class="settings-status-list">
    {#each updateStatusRows(updateStatus) as item (item.label)}
      <div class={item.tone}>
        <dt>{item.label}</dt>
        <dd>{item.value}</dd>
      </div>
    {/each}
  </dl>
  {#if updateStatus?.message}
    <p class="update-hint">{updateStatus.message}</p>
  {/if}
  {#if updateMessage}
    <p class="update-hint update-message">{updateMessage}</p>
  {/if}
  <AutoDismissNotice message={updateError} tone="danger" />
</section>

<style>
  .update-control-row {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) auto;
    align-items: end;
    gap: var(--space-3);
  }

  .update-action-group {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--space-2);
  }

  .update-hint {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.5;
  }

  .update-message {
    color: var(--color-ink);
  }

  @media (max-width: 720px) {
    .update-control-row {
      grid-template-columns: 1fr;
    }

    .update-action-group {
      justify-content: flex-start;
    }
  }
</style>
