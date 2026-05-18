<script lang="ts">
  import { onMount } from 'svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import {
    webApi,
    type ApiError,
    type AutomationOverview,
    type AutomationSummary
  } from '$lib/api/client';
  import type { RepoSummary } from '$lib/viewModels/domain';

  let overview = $state<AutomationOverview | null>(null);
  let repos = $state<RepoSummary[]>([]);
  let loading = $state(true);
  let saving = $state(false);
  let actionId = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let notice = $state<string | null>(null);

  let selectedRepoId = $state('');
  let timezone = $state(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  let dailyHour = $state(9);
  let weeklyHour = $state(10);
  let weekday = $state(0);
  let saveEnabled = $state(false);

  const automations = $derived(overview?.automations ?? []);
  const selectedRepo = $derived(repos.find((repo) => repo.id === selectedRepoId) ?? null);

  onMount(() => {
    void load();
  });

  async function load(): Promise<void> {
    loading = true;
    error = null;
    const [automationResult, repoResult] = await Promise.all([
      webApi.hub.listAutomations(),
      webApi.hub.listRepos()
    ]);
    if (automationResult.ok) overview = automationResult.data;
    else error = automationResult.error;
    if (repoResult.ok) {
      repos = repoResult.data;
      if (!selectedRepoId && repos[0]) selectedRepoId = repos[0].id;
    }
    loading = false;
  }

  async function createSecurityScan(): Promise<void> {
    if (!selectedRepoId || saving) return;
    saving = true;
    notice = null;
    const result = await webApi.hub.createAutomation({
      preset: 'security_scan_pr',
      repo_id: selectedRepoId,
      timezone,
      hour: Number(dailyHour),
      minute: 0,
      enabled: saveEnabled
    });
    saving = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Saved ${result.data.name}${result.data.enabled ? '' : ' paused'}`;
    await load();
  }

  async function createWeeklyTicketFlow(): Promise<void> {
    if (!selectedRepoId || saving) return;
    saving = true;
    notice = null;
    const result = await webApi.hub.createAutomation({
      preset: 'weekly_ticket_flow',
      repo_id: selectedRepoId,
      timezone,
      hour: Number(weeklyHour),
      minute: 0,
      weekday: Number(weekday),
      enabled: saveEnabled
    });
    saving = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Saved ${result.data.name}${result.data.enabled ? '' : ' paused'}`;
    await load();
  }

  async function runNow(automation: AutomationSummary): Promise<void> {
    actionId = automation.id;
    notice = null;
    const result = await webApi.hub.runAutomation(automation.id);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Queued ${automation.name}`;
    await load();
  }

  async function setEnabled(automation: AutomationSummary, enabled: boolean): Promise<void> {
    actionId = automation.id;
    notice = null;
    const result = await webApi.hub.setAutomationEnabled(automation.id, enabled);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${enabled ? 'Resumed' : 'Paused'} ${automation.name}`;
    await load();
  }

  function formatDate(value: string | null): string {
    if (!value) return 'Not scheduled';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  function scheduleLabel(automation: AutomationSummary): string {
    const schedule = automation.schedule;
    if (!schedule) return 'Manual';
    const hour = schedule.schedule.hour;
    const minute = schedule.schedule.minute;
    const time = `${String(hour ?? 0).padStart(2, '0')}:${String(minute ?? 0).padStart(2, '0')}`;
    if (schedule.scheduleKind === 'weekly') {
      const weekdays = Array.isArray(schedule.schedule.weekdays) ? schedule.schedule.weekdays : [0];
      const day = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][Number(weekdays[0] ?? 0)] ?? 'Weekly';
      return `${day} ${time} ${schedule.timezone}`;
    }
    if (schedule.scheduleKind === 'daily') return `Daily ${time} ${schedule.timezone}`;
    return `${schedule.scheduleKind} ${schedule.timezone}`;
  }

  function targetLabel(automation: AutomationSummary): string {
    return String(automation.target.repo_id ?? automation.target.base_repo_id ?? automation.target.worktree_id ?? 'Hub');
  }
</script>

{#snippet heroActions()}
  <button type="button" class="hero-action" onclick={() => void load()} disabled={loading}>Refresh</button>
{/snippet}

<svelte:head>
  <title>Automations</title>
</svelte:head>

<PageHero title="Automations" subtitle="Saved presets for scheduled PMA prompts and ticket flows." actions={heroActions} />

<main class="automations-page">
  {#if error}
    <div class="automation-notice error">{error.message}</div>
  {/if}
  {#if notice}
    <div class="automation-notice success">{notice}</div>
  {/if}

  <section class="automation-section">
    <div class="automation-section-head">
      <h2>New Automation</h2>
      <div class="field-row compact">
        <label>
          <span>Repo</span>
          <select bind:value={selectedRepoId}>
            {#each repos as repo}
              <option value={repo.id}>{repo.name || repo.id}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>Timezone</span>
          <input bind:value={timezone} placeholder="UTC" />
        </label>
        <label class="checkbox-field">
          <input type="checkbox" bind:checked={saveEnabled} />
          <span>Enable on save</span>
        </label>
      </div>
    </div>

    <div class="automation-create-grid">
      <article class="automation-create-card">
        <div>
          <h3>Daily Security Scan</h3>
          <p>Saves a PMA security scan preset for {selectedRepo?.name ?? 'a repo'}. It starts paused until you run or enable it.</p>
        </div>
        <div class="field-row">
          <label>
            <span>Hour</span>
            <input type="number" min="0" max="23" bind:value={dailyHour} />
          </label>
          <button type="button" class="primary-button" disabled={!selectedRepoId || saving} onclick={() => void createSecurityScan()}>
            Save
          </button>
        </div>
      </article>

      <article class="automation-create-card">
        <div>
          <h3>Weekly Preset Ticket Flow</h3>
          <p>Saves a weekly ticket flow preset in a fresh automation worktree from the selected repo's remote default branch.</p>
        </div>
        <div class="field-row">
          <label>
            <span>Day</span>
            <select bind:value={weekday}>
              <option value={0}>Monday</option>
              <option value={1}>Tuesday</option>
              <option value={2}>Wednesday</option>
              <option value={3}>Thursday</option>
              <option value={4}>Friday</option>
              <option value={5}>Saturday</option>
              <option value={6}>Sunday</option>
            </select>
          </label>
          <label>
            <span>Hour</span>
            <input type="number" min="0" max="23" bind:value={weeklyHour} />
          </label>
          <button type="button" class="primary-button" disabled={!selectedRepoId || saving} onclick={() => void createWeeklyTicketFlow()}>
            Save
          </button>
        </div>
      </article>
    </div>
  </section>

  <section class="automation-section">
    <div class="automation-section-head">
      <h2>Monitor</h2>
      {#if overview}
        <div class="summary-strip" aria-label="Automation summary">
          <span>{overview.summary.active} active</span>
          <span>{overview.summary.paused} paused</span>
          <span>{overview.summary.failedJobs} failed</span>
        </div>
      {/if}
    </div>

    {#if loading}
      <div class="empty-state">Loading automations…</div>
    {:else if automations.length === 0}
      <div class="empty-state">No automations yet.</div>
    {:else}
      <div class="automation-table" role="table" aria-label="Automations">
        <div class="automation-row header" role="row">
          <span>Name</span>
          <span>Schedule</span>
          <span>Target</span>
          <span>Last job</span>
          <span>Next run</span>
          <span>Actions</span>
        </div>
        {#each automations as automation}
          <div class="automation-row" role="row">
            <span>
              <strong>{automation.name}</strong>
              <small>{automation.kind} · {automation.enabled ? 'active' : 'paused'}</small>
            </span>
            <span>{scheduleLabel(automation)}</span>
            <span>{targetLabel(automation)}</span>
            <span>
              {#if automation.lastJob}
                {automation.lastJob.state}
                {#if automation.lastJob.ticketFlowRunId}<small>{automation.lastJob.ticketFlowRunId}</small>{/if}
              {:else}
                No runs
              {/if}
            </span>
            <span>{formatDate(automation.schedule?.nextFireAt ?? null)}</span>
            <span class="action-cell">
              <button type="button" class="ghost-button" disabled={actionId === automation.id} onclick={() => void runNow(automation)}>Run</button>
              <button
                type="button"
                class="ghost-button"
                disabled={actionId === automation.id}
                onclick={() => void setEnabled(automation, !automation.enabled)}
              >
                {automation.enabled ? 'Pause' : 'Resume'}
              </button>
            </span>
          </div>
        {/each}
      </div>
    {/if}
  </section>
</main>

<style>
  .automations-page {
    display: grid;
    gap: 24px;
    padding: 0 24px 32px;
  }

  .automation-section {
    display: grid;
    gap: 16px;
  }

  .automation-section-head {
    align-items: end;
    display: flex;
    gap: 16px;
    justify-content: space-between;
  }

  .automation-section h2,
  .automation-create-card h3 {
    margin: 0;
  }

  .automation-create-grid {
    display: grid;
    gap: 16px;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  }

  .automation-create-card {
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    display: grid;
    gap: 16px;
    padding: 16px;
  }

  .automation-create-card p,
  .automation-row small {
    color: var(--color-ink-muted);
    margin: 6px 0 0;
  }

  .field-row {
    align-items: end;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }

  .field-row.compact {
    justify-content: end;
  }

  label {
    display: grid;
    gap: 6px;
    min-width: 150px;
  }

  label span {
    color: var(--color-ink-muted);
    font-size: 0.82rem;
  }

  .checkbox-field {
    align-items: center;
    display: flex;
    gap: 8px;
    min-height: 36px;
  }

  .checkbox-field input {
    min-height: auto;
    padding: 0;
  }

  input,
  select {
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    color: var(--color-ink);
    min-height: 36px;
    padding: 0 10px;
  }

  .summary-strip {
    color: var(--color-ink-muted);
    display: flex;
    gap: 12px;
  }

  .automation-table {
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    overflow: hidden;
  }

  .automation-row {
    align-items: center;
    border-top: 1px solid var(--color-border-subtle);
    display: grid;
    gap: 12px;
    grid-template-columns: minmax(220px, 1.4fr) minmax(160px, 1fr) minmax(110px, 0.8fr) minmax(100px, 0.8fr) minmax(160px, 1fr) minmax(132px, auto);
    padding: 12px 14px;
  }

  .automation-row.header {
    background: var(--color-surface-muted);
    border-top: 0;
    color: var(--color-ink-muted);
    font-size: 0.82rem;
    font-weight: 600;
  }

  .automation-row strong,
  .automation-row small {
    display: block;
  }

  .action-cell {
    display: flex;
    gap: 8px;
  }

  .automation-notice,
  .empty-state {
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    padding: 12px 14px;
  }

  .automation-notice.error {
    background: var(--color-danger-soft);
    border-color: var(--color-danger);
    color: var(--color-danger);
  }

  .automation-notice.success {
    background: var(--color-success-soft);
    border-color: var(--color-success);
    color: var(--color-success);
  }

  @media (max-width: 980px) {
    .automation-row,
    .automation-row.header {
      grid-template-columns: 1fr;
    }

    .automation-row.header {
      display: none;
    }

    .automation-section-head {
      align-items: stretch;
      flex-direction: column;
    }

    .field-row.compact {
      justify-content: start;
    }
  }
</style>
