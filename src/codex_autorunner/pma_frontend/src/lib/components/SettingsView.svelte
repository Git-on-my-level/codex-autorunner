<script lang="ts">
  import type {
    SettingsAgentStatus,
    SettingsSensitiveAction,
    SettingsSessionState,
    SettingsStatusItem,
    SettingsViewModel
  } from '$lib/viewModels/settings';
  import type { SensitiveApprovalRequest } from '$lib/viewModels/domain';
  import SensitiveApprovalCard from './SensitiveApprovalCard.svelte';

  let {
    state,
    view = null,
    errorMessage = null,
    saveError = null,
    pendingAction = null,
    onSessionChange,
    onRequestSensitiveAction,
    onConfirmSensitiveAction,
    onCancelSensitiveAction,
    onApprovalDecision
  }: {
    state: 'loading' | 'error' | 'ready';
    view?: SettingsViewModel | null;
    errorMessage?: string | null;
    saveError?: string | null;
    pendingAction?: SettingsSensitiveAction | null;
    onSessionChange?: (session: SettingsSessionState) => void;
    onRequestSensitiveAction?: (action: SettingsSensitiveAction) => void;
    onConfirmSensitiveAction?: (action: SettingsSensitiveAction) => void;
    onCancelSensitiveAction?: () => void;
    onApprovalDecision?: (approval: SensitiveApprovalRequest, decision: 'approve' | 'decline') => void;
  } = $props();

  function patchSession(key: keyof SettingsSessionState, value: string): void {
    if (!view) return;
    onSessionChange?.({ ...view.session, [key]: value });
  }
</script>

<section class="page-stack settings-page">
  <div class="section-heading detail-heading">
    <div>
      <p class="eyebrow">Local mode</p>
      <h1>Settings</h1>
      <p>Operational hub status, agent/model readiness, attachments, and sensitive CAR actions.</p>
    </div>
  </div>

  {#if state === 'loading'}
    <div class="state-panel">Loading settings...</div>
  {:else if state === 'error'}
    <div class="state-panel error">Could not load settings. {errorMessage}</div>
  {:else if view}
    <section class="page-panel settings-panel">
      <div class="panel-heading-row">
        <h2>Hub</h2>
        <span class="status-pill idle">Local</span>
      </div>
      {@render statusList(view.hub)}
    </section>

    <section class="page-panel settings-panel">
      <div class="panel-heading-row">
        <h2>PMA agent/model</h2>
        <button type="button" onclick={() => onRequestSensitiveAction?.({
          id: 'update-runtime-preferences',
          label: 'Update runtime preferences',
          description: 'Save PMA model and run-limit overrides for future autorunner work.',
          available: true,
          reason: 'This writes session settings through /api/session/settings.'
        })}>Save preferences</button>
      </div>
      <div class="settings-form-grid">
        <label>
          <span>Model override</span>
          <input
            value={view.session.modelOverride}
            placeholder="Use server default"
            oninput={(event) => patchSession('modelOverride', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Reasoning effort</span>
          <input
            value={view.session.effortOverride}
            placeholder="Use server default"
            oninput={(event) => patchSession('effortOverride', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Stop after runs</span>
          <input
            inputmode="numeric"
            value={view.session.stopAfterRuns}
            placeholder="No limit"
            oninput={(event) => patchSession('stopAfterRuns', event.currentTarget.value)}
          />
        </label>
      </div>
      {#if saveError}
        <p class="compose-error">{saveError}</p>
      {/if}
      <p class="permission-note">PMA has full permission for normal coding work. Sensitive CAR operations require approval.</p>
      {@render agentList(view.pmaAgents, 'No PMA-capable agents are visible from the server.')}
    </section>

    <section class="page-panel settings-panel">
      <h2>Coding agents</h2>
      {@render agentList(view.codingAgents, 'No additional coding agents are visible from the server.')}
    </section>

    <div class="settings-grid">
      <section class="page-panel settings-panel">
        <h2>Integrations</h2>
        {@render statusList(view.integrations)}
      </section>

      <section class="page-panel settings-panel">
        <h2>Filebox/attachments</h2>
        {@render statusList(view.filebox)}
      </section>

      <section class="page-panel settings-panel">
        <h2>Secrets</h2>
        {@render statusList(view.secrets)}
      </section>
    </div>

    <section class="page-panel settings-panel">
      <div class="panel-heading-row">
        <h2>Sensitive CAR actions</h2>
        <span class="status-pill waiting">{view.approvals.length} pending</span>
      </div>
      {#if view.approvals.length === 0}
        <p>No sensitive CAR approvals are waiting.</p>
      {:else}
        <div class="settings-approval-list">
          {#each view.approvals as approval (approval.id)}
            <SensitiveApprovalCard {approval} onDecision={onApprovalDecision} />
          {/each}
        </div>
      {/if}
      <div class="sensitive-action-grid">
        {#each view.sensitiveActions as action}
          <article class={`sensitive-action ${action.available ? 'available' : 'unavailable'}`}>
            <div>
              <strong>{action.label}</strong>
              <p>{action.description}</p>
              <span>{action.available ? action.reason : `Unavailable: ${action.reason}`}</span>
            </div>
            {#if action.available}
              <button type="button" onclick={() => onRequestSensitiveAction?.(action)}>Review</button>
            {/if}
          </article>
        {/each}
      </div>
    </section>

    <details class="page-panel settings-panel advanced-panel">
      <summary>Advanced/debug</summary>
      {@render statusList(view.advanced)}
    </details>
  {/if}
</section>

{#if pendingAction}
  <div class="modal-backdrop" role="presentation">
    <div class="approval-modal" role="dialog" aria-modal="true" aria-labelledby="settings-approval-title">
      <span class="approval-type">Sensitive settings approval</span>
      <h2 id="settings-approval-title">{pendingAction.label}</h2>
      <p>{pendingAction.description}</p>
      <p class="muted">{pendingAction.reason}</p>
      <div class="approval-actions">
        <button type="button" onclick={() => onCancelSensitiveAction?.()}>Cancel</button>
        <button class="danger-action" type="button" onclick={() => onConfirmSensitiveAction?.(pendingAction)}>
          Approve change
        </button>
      </div>
    </div>
  </div>
{/if}

{#snippet statusList(items: SettingsStatusItem[])}
  <dl class="settings-status-list">
    {#each items as item}
      <div class={item.tone}>
        <dt>{item.label}</dt>
        <dd>{item.value}</dd>
      </div>
    {/each}
  </dl>
{/snippet}

{#snippet agentList(agents: SettingsAgentStatus[], emptyText: string)}
  {#if agents.length === 0}
    <p>{emptyText}</p>
  {:else}
    <div class="agent-status-list">
      {#each agents as agent}
        <article class="agent-status-row">
          <div>
            <strong>{agent.name}</strong>
            <span>{agent.id} · {agent.providerLabel}</span>
          </div>
          <span class={`status-pill ${agent.modelStatus === 'available' ? 'done' : agent.modelStatus === 'unavailable' ? 'waiting' : 'idle'}`}>
            {agent.modelLabel}
          </span>
        </article>
      {/each}
    </div>
  {/if}
{/snippet}
