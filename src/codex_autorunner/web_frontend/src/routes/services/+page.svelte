<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import OverflowMenu from '$lib/components/OverflowMenu.svelte';
  import type { OverflowMenuItem } from '$lib/components/OverflowMenu';
  import { confirmDialog, confirmDialogTyped } from '$lib/components/confirmDialog';
  import {
    webApi,
    type ApiError,
    type PreviewServiceAction,
    type PreviewServiceLogs
  } from '$lib/api/client';
  import type {
    PreviewServiceKind,
    PreviewServiceReadModel,
    PreviewServiceStatus,
    PreviewServicesReadModel
  } from '$lib/api/readModelContracts';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    defaultServiceFilters,
    filterServices,
    serviceActionEligibility,
    serviceClassLabel,
    serviceCounts,
    serviceKindLabel,
    serviceNeedsAttention,
    serviceOwnershipLabel,
    serviceScopeLabel,
    serviceStatusLabel,
    serviceTargetLabel,
    serviceTrustLabel,
    serviceUptimeLabel,
    type ServiceFilters
  } from '$lib/viewModels/services';
  import {
    ensureServicesLoaded,
    readModelEntityStore,
    selectPreviewServicesReadModel
  } from '$lib/data';

  let { data = { status: 'cold' as const, tags: [] } } = $props();
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  const model = $derived<PreviewServicesReadModel | null>(selectPreviewServicesReadModel(readModelState));
  const hasCachedServices = $derived(Boolean(model));
  let refreshing = $state(false);
  let coldHydrating = $state(true);
  const loading = $derived((data.status === 'cold' && coldHydrating && !hasCachedServices) || (refreshing && !hasCachedServices));
  let actionId = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let notice = $state<string | null>(null);
  let filters = $state<ServiceFilters>(defaultServiceFilters());
  let selectedLogs = $state<PreviewServiceLogs | null>(null);
  let selectedLogService = $state<PreviewServiceReadModel | null>(null);
  let logsLoadingId = $state<string | null>(null);
  let copiedServiceId = $state<string | null>(null);
  let issuedLinks = $state<Record<string, string>>({});
  let createMode = $state<'static' | 'loopback' | 'managed'>('static');
  let showRegister = $state(false);
  let editService = $state<PreviewServiceReadModel | null>(null);
  let createForm = $state({
    name: '',
    path: '',
    url: '',
    command: '',
    cwd: '',
    healthPath: '/',
    scope: '',
    port: '',
    env: '',
    envPolicy: 'minimal',
    serviceClass: 'preview',
    trustLevel: 'generated',
    ownership: 'static',
    autostart: false,
    start: false
  });
  let editForm = $state({
    name: '',
    command: '',
    cwd: '',
    healthPath: '/',
    scope: '',
    port: '',
    env: '',
    envPolicy: 'minimal',
    serviceClass: 'preview',
    trustLevel: 'generated',
    ownership: 'static',
    autostart: false
  });
  let copyTimer: ReturnType<typeof setTimeout> | null = null;

  const counts = $derived(serviceCounts(model));
  const services = $derived(model?.services ?? []);
  const filteredServices = $derived(filterServices(services, filters));
  const hasFilters = $derived(
    Boolean(
      filters.query.trim() ||
        filters.scope.trim() ||
        filters.status !== 'all' ||
        filters.kind !== 'all' ||
        filters.serviceClass !== 'all'
    )
  );
  const classChips = $derived(
    (['preview', 'application', 'infrastructure'] as const)
      .map((value) => ({ value, label: serviceClassLabel(value), count: counts[value] }))
      .filter((chip) => chip.count > 0)
  );

  function toggleClassFilter(value: ServiceFilters['serviceClass']): void {
    filters.serviceClass = filters.serviceClass === value ? 'all' : value;
  }

  const createModeHints: Record<typeof createMode, string> = {
    static: 'Serve a file or directory through the hub proxy.',
    loopback: 'Proxy an already-running local URL through the hub.',
    managed: 'CAR starts and supervises the process for you.'
  };
  const createModeHint = $derived(createModeHints[createMode]);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadServices({ showLoading: data.status === 'cold' && !hasCachedServices });
    return () => {
      if (copyTimer) clearTimeout(copyTimer);
    };
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadServices(options: { showLoading?: boolean } = {}): Promise<void> {
    if (options.showLoading !== false) refreshing = true;
    error = null;
    try {
      const result = await ensureServicesLoaded({ refresh: true });
      if (result.status === 'error') {
        error = result.error;
      }
    } finally {
      coldHydrating = false;
      refreshing = false;
    }
  }

  async function refreshServices(): Promise<void> {
    const result = await ensureServicesLoaded({ refresh: true });
    if (result.status === 'error') {
      error = result.error;
    }
  }

  async function runLifecycle(service: PreviewServiceReadModel, action: PreviewServiceAction): Promise<void> {
    actionId = `${action}:${service.serviceId}`;
    error = null;
    notice = null;
    const result = await webApi.hub.serviceAction(service.serviceId, action);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${actionLabel(action)} ${service.name}`;
    await refreshServices();
  }

  async function registerService(): Promise<void> {
    actionId = `create:${createMode}`;
    error = null;
    notice = null;
    const common = {
      name: createForm.name.trim() || null,
      scope_links: parseScopeLinks(createForm.scope),
      created_by: 'ui',
      service_class: createForm.serviceClass,
      trust_level: createForm.trustLevel,
      ownership: createForm.ownership
    };
    const result =
      createMode === 'static'
        ? await webApi.hub.registerStaticService({
            ...common,
            path: createForm.path.trim(),
            kind: createForm.path.trim().includes('.') ? 'static_file' : 'static_dir'
          })
        : createMode === 'loopback'
          ? await webApi.hub.registerLoopbackService({
              ...common,
              url: createForm.url.trim(),
              health_path: createForm.healthPath.trim()
            })
          : await webApi.hub.registerManagedService({
              ...common,
              name: createForm.name.trim() || 'Managed service',
              argv: shellWords(createForm.command),
              cwd: createForm.cwd.trim(),
              env: envPairs(createForm.env),
              env_policy: createForm.envPolicy,
              port_policy: portPolicy(createForm.port),
              health_check: createForm.healthPath.trim()
                ? { type: 'http', path: createForm.healthPath.trim() }
                : { type: 'tcp' },
              auto_start_on_hub_start: createForm.autostart,
              start: createForm.start
            });
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Registered ${result.data.name}`;
    createForm = { ...createForm, name: '', path: '', url: '', command: '', port: '', env: '' };
    await refreshServices();
  }

  async function runDestructive(service: PreviewServiceReadModel, action: 'kill' | 'teardown' | 'unlink'): Promise<void> {
    const typed = await confirmDialogTyped({
      title: destructiveTitle(action),
      message: destructiveMessage(service, action),
      confirmText: destructiveConfirmText(action),
      cancelText: 'Cancel',
      danger: true,
      requireType: service.serviceId
    });
    if (typed !== service.serviceId) return;
    actionId = `${action}:${service.serviceId}`;
    error = null;
    notice = null;
    const shouldForce = action === 'kill' || (action === 'unlink' && serviceActionEligibility(service).requiresForceForUnlink);
    const result = await webApi.hub.serviceAction(
      service.serviceId,
      action,
      shouldForce
        ? {
            force: true,
            forceAttestation: `${destructiveConfirmText(action)} requested from Services UI for ${service.serviceId}.`
          }
        : undefined
    );
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${destructiveConfirmText(action)} ${service.name}`;
    if (selectedLogService?.serviceId === service.serviceId && action !== 'kill') {
      selectedLogService = null;
      selectedLogs = null;
    }
    await refreshServices();
  }

  async function unlinkService(service: PreviewServiceReadModel): Promise<void> {
    if (!serviceActionEligibility(service).canUnlink) {
      await runDestructive(service, 'unlink');
      return;
    }
    const confirmed = await confirmDialog({
      title: 'Unlink service',
      message: `Remove ${service.name} from the preview service registry? This does not stop non-CAR-owned processes.`,
      confirmText: 'Unlink',
      cancelText: 'Cancel',
      danger: true
    });
    if (!confirmed) return;
    actionId = `unlink:${service.serviceId}`;
    error = null;
    notice = null;
    const result = await webApi.hub.serviceAction(service.serviceId, 'unlink');
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Unlinked ${service.name}`;
    await refreshServices();
  }

  async function viewLogs(service: PreviewServiceReadModel): Promise<void> {
    logsLoadingId = service.serviceId;
    error = null;
    const result = await webApi.hub.getServiceLogs(service.serviceId, 200);
    logsLoadingId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    selectedLogService = service;
    selectedLogs = result.data;
  }

  async function copyCarUrl(service: PreviewServiceReadModel): Promise<void> {
    const url = await issueServiceUrl(service);
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      copiedServiceId = service.serviceId;
      if (copyTimer) clearTimeout(copyTimer);
      copyTimer = setTimeout(() => {
        copiedServiceId = null;
      }, 1500);
    } catch {
      notice = 'Copy failed';
    }
  }

  async function openCarUrl(service: PreviewServiceReadModel): Promise<void> {
    const url = await issueServiceUrl(service);
    if (!url) return;
    window.open(href(url), '_blank', 'noopener,noreferrer');
  }

  async function issueServiceUrl(service: PreviewServiceReadModel): Promise<string | null> {
    actionId = `issue-link:${service.serviceId}`;
    error = null;
    const result = await webApi.hub.issueServiceLink(service.serviceId, 86400);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return null;
    }
    const url = absolutePreviewUrl(result.data.previewUrl);
    issuedLinks = { ...issuedLinks, [service.serviceId]: url };
    return url;
  }

  async function issueLink(service: PreviewServiceReadModel): Promise<void> {
    const url = await issueServiceUrl(service);
    if (!url) return;
    await navigator.clipboard.writeText(url);
    copiedServiceId = service.serviceId;
    notice = `Issued preview link for ${service.name}`;
  }

  async function revokeLinks(service: PreviewServiceReadModel): Promise<void> {
    const confirmed = await confirmDialog({
      title: 'Revoke preview links',
      message: `Revoke active capability links for ${service.name}?`,
      confirmText: 'Revoke',
      cancelText: 'Cancel',
      danger: true
    });
    if (!confirmed) return;
    actionId = `revoke-link:${service.serviceId}`;
    const result = await webApi.hub.revokeServiceLinks(service.serviceId);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    const next = { ...issuedLinks };
    delete next[service.serviceId];
    issuedLinks = next;
    notice = `Revoked ${result.data.revoked} preview link${result.data.revoked === 1 ? '' : 's'}`;
  }

  async function toggleAutostart(service: PreviewServiceReadModel): Promise<void> {
    const current = service.restartPolicy?.auto_start_on_hub_start === true;
    actionId = `autostart:${service.serviceId}`;
    const result = await webApi.hub.updateService(service.serviceId, {
      restart_policy: { auto_start_on_hub_start: !current, restart_on_exit: 'never' }
    });
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `${!current ? 'Enabled' : 'Disabled'} autostart for ${service.name}`;
    await refreshServices();
  }

  function beginEdit(service: PreviewServiceReadModel): void {
    editService = service;
    const command = service.desiredState.command as Record<string, unknown> | undefined;
    const port = service.desiredState.port_policy as Record<string, unknown> | undefined;
    editForm = {
      name: service.name,
      command: Array.isArray(command?.argv) ? command.argv.map(String).join(' ') : '',
      cwd: typeof command?.cwd === 'string' ? command.cwd : '',
      healthPath: typeof service.healthCheck?.path === 'string' ? service.healthCheck.path : '/',
      scope: service.scope ?? '',
      port: typeof port?.port === 'number' ? String(port.port) : '',
      env: '',
      envPolicy: typeof command?.env_policy === 'string' ? command.env_policy : 'minimal',
      serviceClass: service.serviceClass,
      trustLevel: service.trustLevel,
      ownership: service.ownership,
      autostart: service.restartPolicy?.auto_start_on_hub_start === true
    };
  }

  async function saveEdit(): Promise<void> {
    if (!editService) return;
    actionId = `edit:${editService.serviceId}`;
    const payload: Record<string, unknown> = {
      name: editForm.name.trim(),
      scope_links: parseScopeLinks(editForm.scope),
      service_class: editForm.serviceClass,
      trust_level: editForm.trustLevel,
      ownership: editForm.ownership,
      restart_policy: { auto_start_on_hub_start: editForm.autostart, restart_on_exit: 'never' }
    };
    if (editService.kind === 'managed_command') {
      const commandPayload: Record<string, unknown> = {
        argv: shellWords(editForm.command),
        cwd: editForm.cwd.trim(),
        env_policy: editForm.envPolicy
      };
      if (editForm.env.trim()) {
        commandPayload.env = envPairs(editForm.env);
      }
      payload.command = commandPayload;
      payload.port_policy = portPolicy(editForm.port);
      payload.health_check = editForm.healthPath.trim()
        ? { type: 'http', path: editForm.healthPath.trim() }
        : { type: 'tcp' };
    }
    const result = await webApi.hub.updateService(editService.serviceId, payload);
    actionId = null;
    if (!result.ok) {
      error = result.error;
      return;
    }
    notice = `Updated ${result.data.name}`;
    editService = null;
    await refreshServices();
  }

  function clearFilters(): void {
    filters = defaultServiceFilters();
  }

  function actionBusy(service: PreviewServiceReadModel, action: string): boolean {
    return actionId === `${action}:${service.serviceId}`;
  }

  function actionLabel(action: PreviewServiceAction): string {
    const labels: Record<PreviewServiceAction, string> = {
      start: 'Started',
      stop: 'Stopped',
      restart: 'Restarted',
      health: 'Checked',
      kill: 'Killed',
      teardown: 'Tore down',
      unlink: 'Unlinked'
    };
    return labels[action];
  }

  function destructiveTitle(action: 'kill' | 'teardown' | 'unlink'): string {
    return action === 'kill' ? 'Kill service' : action === 'teardown' ? 'Teardown service' : 'Force unlink service';
  }

  function destructiveConfirmText(action: 'kill' | 'teardown' | 'unlink'): string {
    return action === 'kill' ? 'Kill' : action === 'teardown' ? 'Teardown' : 'Unlink';
  }

  function destructiveMessage(service: PreviewServiceReadModel, action: 'kill' | 'teardown' | 'unlink'): string {
    if (action === 'kill') {
      return `Force terminate ${service.name}. This sends a kill request to the CAR-owned process group.`;
    }
    if (action === 'teardown') {
      return `Stop or kill ${service.name} as needed, then remove the registry entry.`;
    }
    return `Remove the registry entry for running service ${service.name}. The process may keep running outside the registry.`;
  }

  function healthLabel(service: PreviewServiceReadModel): string {
    if (service.status === 'healthy' || service.status === 'unhealthy') return serviceStatusLabel(service.status);
    const type = typeof service.healthCheck?.type === 'string' ? service.healthCheck.type : '';
    const path = typeof service.healthCheck?.path === 'string' ? service.healthCheck.path : '';
    return type ? `${type}${path ? ` ${path}` : ''}` : '—';
  }

  function absolutePreviewUrl(url: string): string {
    return new URL(href(url), window.location.origin).toString();
  }

  function parseScopeLinks(scope: string): Array<{ kind: string; id?: string; path?: string }> {
    return scope
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => {
        if (!item.includes(':')) return { kind: item };
        const [kind, value] = item.split(/:(.*)/s);
        return kind === 'workspace' ? { kind, path: value } : { kind, id: value };
      });
  }

  function shellWords(command: string): string[] {
    return command.trim().split(/\s+/).filter(Boolean);
  }

  function envPairs(raw: string): Record<string, string> {
    const env: Record<string, string> = {};
    for (const line of raw.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.includes('=')) continue;
      const [key, ...rest] = trimmed.split('=');
      env[key.trim()] = rest.join('=');
    }
    return env;
  }

  function portPolicy(port: string): Record<string, unknown> {
    const parsed = Number(port);
    return Number.isInteger(parsed) && parsed > 0 ? { mode: 'preferred', port: parsed } : { mode: 'auto' };
  }

  function primaryLifecycle(
    service: PreviewServiceReadModel
  ): { action: PreviewServiceAction; label: string } | null {
    const eligibility = serviceActionEligibility(service);
    if (eligibility.canStop) return { action: 'stop', label: 'Stop' };
    if (eligibility.canStart) return { action: 'start', label: 'Start' };
    return null;
  }

  function moreActions(service: PreviewServiceReadModel): OverflowMenuItem[] {
    const eligibility = serviceActionEligibility(service);
    const items: OverflowMenuItem[] = [];
    if (eligibility.canRestart) {
      items.push({ label: 'Restart', disabled: actionBusy(service, 'restart'), onSelect: () => void runLifecycle(service, 'restart') });
    }
    items.push({ label: 'Run health check', disabled: actionBusy(service, 'health'), onSelect: () => void runLifecycle(service, 'health') });
    if (eligibility.canOpen) {
      items.push({ label: 'Issue preview link', disabled: actionBusy(service, 'issue-link'), onSelect: () => void issueLink(service) });
      items.push({ label: 'Revoke preview links', disabled: actionBusy(service, 'revoke-link'), onSelect: () => void revokeLinks(service) });
    }
    const autostartOn = service.restartPolicy?.auto_start_on_hub_start === true;
    items.push({
      label: autostartOn ? 'Disable autostart' : 'Enable autostart',
      disabled: actionBusy(service, 'autostart'),
      onSelect: () => void toggleAutostart(service)
    });
    const editLocked =
      service.status === 'running' || service.status === 'healthy' || service.status === 'starting' || service.status === 'unhealthy';
    items.push({ label: 'Edit', disabled: editLocked, onSelect: () => beginEdit(service) });
    if (eligibility.canKill) {
      items.push({ label: 'Kill', danger: true, disabled: actionBusy(service, 'kill'), onSelect: () => void runDestructive(service, 'kill') });
    }
    if (eligibility.canTeardown) {
      items.push({ label: 'Teardown', danger: true, disabled: actionBusy(service, 'teardown'), onSelect: () => void runDestructive(service, 'teardown') });
    }
    if (eligibility.canUnlink || eligibility.requiresForceForUnlink) {
      items.push({ label: 'Unlink', danger: true, disabled: actionBusy(service, 'unlink'), onSelect: () => void unlinkService(service) });
    }
    return items;
  }

  const statusOptions: Array<ServiceFilters['status']> = ['all', 'registered', 'starting', 'running', 'healthy', 'unhealthy', 'stopped', 'exited', 'failed', 'orphaned', 'conflict'];
  const kindOptions: Array<ServiceFilters['kind']> = ['all', 'static_file', 'static_dir', 'loopback_url', 'managed_command'];
</script>

<AutoDismissNotice message={notice} tone="success" />

<section class="services-page page-stack">
  <PageHero title="Services" subtitle="Preview service registry, links, process state, and logs.">
    {#snippet stats()}
      {#if model}
        <dl class="hero-stats">
          <div><dd>{counts.total}</dd><dt>Total</dt></div>
          <div class:active={counts.running > 0}><dd>{counts.running}</dd><dt>Running</dt></div>
          {#if counts.attention > 0}
            <div class="danger"><dd>{counts.attention}</dd><dt>Attention</dt></div>
          {/if}
        </dl>
      {/if}
    {/snippet}
    {#snippet actions()}
      {#if model && services.length > 0}
        <button class="ghost-button" type="button" onclick={() => (showRegister = !showRegister)}>
          {showRegister ? 'Hide register' : '+ Register service'}
        </button>
      {/if}
      <button class="ghost-button" type="button" onclick={() => loadServices()} disabled={loading}>Refresh</button>
    {/snippet}
  </PageHero>

  {#if loading}
    <ContentSkeleton variant="index" rows={5} />
  {:else if error && !model}
    <div class="state-panel error">
      <strong>Could not load services</strong>
      <p>{error.message}</p>
      <button class="ghost-button" type="button" onclick={() => loadServices()}>Retry</button>
    </div>
  {:else}
    {#if error}
      <div class="state-panel error">
        <strong>Action failed</strong>
        <p>{error.message}</p>
      </div>
    {/if}
    {#if showRegister || services.length === 0}
    <section class="service-editor" aria-label="Register service">
      <div class="editor-head">
        <div class="editor-head-copy">
          <h2>Register a service</h2>
          {#if services.length === 0}
            <p class="editor-sub">No services yet. Register your first preview service to proxy it through the hub.</p>
          {/if}
        </div>
        <div class="mode-tabs" role="tablist" aria-label="Service kind">
          <button role="tab" aria-selected={createMode === 'static'} class:active={createMode === 'static'} type="button" onclick={() => createMode = 'static'}>Static</button>
          <button role="tab" aria-selected={createMode === 'loopback'} class:active={createMode === 'loopback'} type="button" onclick={() => createMode = 'loopback'}>Loopback</button>
          <button role="tab" aria-selected={createMode === 'managed'} class:active={createMode === 'managed'} type="button" onclick={() => createMode = 'managed'}>Managed</button>
        </div>
      </div>
      <p class="editor-hint">{createModeHint}</p>
      <div class="editor-grid">
        <label><span>Name</span><input bind:value={createForm.name} placeholder="frontend preview" /></label>
        {#if createMode === 'static'}
          <label class="wide"><span>Path</span><input bind:value={createForm.path} placeholder="/absolute/path/to/file-or-dir" /></label>
        {:else if createMode === 'loopback'}
          <label class="wide"><span>URL</span><input bind:value={createForm.url} placeholder="http://127.0.0.1:5173/" /></label>
          <label><span>Health path</span><input bind:value={createForm.healthPath} placeholder="/" /></label>
        {:else}
          <label class="wide"><span>Command</span><input bind:value={createForm.command} placeholder="npm run dev -- --host 127.0.0.1 --port $PORT" /></label>
          <label><span>CWD</span><input bind:value={createForm.cwd} placeholder="/path/to/worktree" /></label>
          <label><span>Port</span><input bind:value={createForm.port} inputmode="numeric" placeholder="auto" /></label>
          <label><span>Health path</span><input bind:value={createForm.healthPath} placeholder="/" /></label>
          <label><span>Env policy</span><select bind:value={createForm.envPolicy}><option value="minimal">Minimal</option><option value="allowlist">Allowlist</option><option value="inherit_all">Inherit all</option></select></label>
          <label class="wide"><span>Env</span><textarea bind:value={createForm.env} rows="2" placeholder="KEY=value"></textarea></label>
        {/if}
        <label><span>Scope</span><input bind:value={createForm.scope} placeholder="repo:car, ticket:tkt_1" /></label>
        <label><span>Class</span><select bind:value={createForm.serviceClass}><option value="preview">Preview</option><option value="application">Application</option><option value="infrastructure">Infrastructure</option></select></label>
        <label><span>Trust</span><select bind:value={createForm.trustLevel}><option value="generated">Generated</option><option value="trusted">Trusted</option><option value="external">External</option></select></label>
        <label><span>Ownership</span><select bind:value={createForm.ownership}><option value="static">Static</option><option value="car_managed">CAR-managed</option><option value="external">External</option></select></label>
        {#if createMode === 'managed'}
          <label class="check-row"><input type="checkbox" bind:checked={createForm.start} /> <span>Start after registering</span></label>
          <label class="check-row"><input type="checkbox" bind:checked={createForm.autostart} /> <span>Autostart on hub start</span></label>
        {/if}
      </div>
      <button class="primary-button" type="button" onclick={registerService} disabled={Boolean(actionId?.startsWith('create:'))}>Register service</button>
    </section>
    {/if}

    {#if services.length > 0}
    {#if classChips.length > 0}
      <div class="class-chips" role="group" aria-label="Filter by class">
        {#each classChips as chip (chip.value)}
          <button
            type="button"
            class={`class-chip class-${chip.value}`}
            class:active={filters.serviceClass === chip.value}
            aria-pressed={filters.serviceClass === chip.value}
            onclick={() => toggleClassFilter(chip.value)}
          >
            <span class="class-chip-label">{chip.label}</span>
            <span class="class-chip-count">{chip.count}</span>
          </button>
        {/each}
      </div>
    {/if}
    <div class="services-filters" aria-label="Service filters">
      <label>
        <span>Search</span>
        <input type="search" bind:value={filters.query} placeholder="name, id, url" />
      </label>
      <label>
        <span>Status</span>
        <select bind:value={filters.status}>
          {#each statusOptions as status}
            <option value={status}>{status === 'all' ? 'All statuses' : serviceStatusLabel(status as PreviewServiceStatus)}</option>
          {/each}
        </select>
      </label>
      <label>
        <span>Kind</span>
        <select bind:value={filters.kind}>
          {#each kindOptions as kind}
            <option value={kind}>{kind === 'all' ? 'All kinds' : serviceKindLabel(kind as PreviewServiceKind)}</option>
          {/each}
        </select>
      </label>
      <label>
        <span>Scope</span>
        <input type="search" bind:value={filters.scope} placeholder="repo:car" />
      </label>
      <button class="ghost-button" type="button" onclick={clearFilters} disabled={!hasFilters}>Clear</button>
    </div>

    {#if filteredServices.length === 0}
      <div class="empty-state">
        <strong>No services match the current filters</strong>
        <p>Clear filters or adjust status, kind, scope, or search.</p>
      </div>
    {:else}
      <div class="services-table-wrap">
        <table class="services-table">
          <thead>
            <tr>
              <th>Service</th>
              <th>Status</th>
              <th>Health</th>
              <th>Class</th>
              <th>Target</th>
              <th>Uptime</th>
              <th class="col-pid">PID</th>
              <th class="col-links">Links</th>
              <th class="col-actions">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each filteredServices as service (service.serviceId)}
              {@const eligibility = serviceActionEligibility(service)}
              {@const lifecycle = primaryLifecycle(service)}
              {@const scopeLabel = serviceScopeLabel(service)}
              <tr class:attention={serviceNeedsAttention(service)}>
                <td>
                  <div class="service-name">
                    <strong title={service.name}>{service.name}</strong>
                    <div class="service-meta">
                      <span class="id-tag">{service.serviceId}</span>
                      <span class="meta-dot" aria-hidden="true">·</span>
                      <span>{serviceKindLabel(service.kind)}</span>
                      {#if scopeLabel && scopeLabel !== '—'}
                        <span class="meta-dot" aria-hidden="true">·</span>
                        <span class="truncate">{scopeLabel}</span>
                      {/if}
                    </div>
                  </div>
                </td>
                <td><span class={`status-pill svc-status status-${service.status}`}>{serviceStatusLabel(service.status)}</span></td>
                <td class="cell-muted">{healthLabel(service)}</td>
                <td>
                  <div class="badge-stack">
                    <span class={`meta-badge class-${service.serviceClass}`}>{serviceClassLabel(service.serviceClass)}</span>
                    <span class="meta-badge">{serviceTrustLabel(service.trustLevel)}</span>
                    <span class="meta-badge">{serviceOwnershipLabel(service.ownership)}</span>
                  </div>
                </td>
                <td><span class="truncate cell-muted">{serviceTargetLabel(service)}</span></td>
                <td class="cell-num">{serviceUptimeLabel(service)}</td>
                <td class="cell-num col-pid">{service.ownerPid ?? '—'}</td>
                <td class="col-links">
                  {#if eligibility.canOpen}
                    <div class="link-actions">
                      <button class="ghost-button compact" type="button" onclick={() => openCarUrl(service)}>Open</button>
                      <button class="ghost-button compact" type="button" onclick={() => copyCarUrl(service)} disabled={actionBusy(service, 'issue-link')}>
                        {copiedServiceId === service.serviceId ? 'Copied' : 'Copy'}
                      </button>
                    </div>
                  {:else}
                    <span class="cell-muted">—</span>
                  {/if}
                </td>
                <td class="col-actions">
                  <div class="row-actions">
                    {#if lifecycle}
                      <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, lifecycle.action)} disabled={actionBusy(service, lifecycle.action)}>{lifecycle.label}</button>
                    {/if}
                    <button class="ghost-button compact" type="button" onclick={() => viewLogs(service)} disabled={!eligibility.canViewLogs || logsLoadingId === service.serviceId}>Logs</button>
                    <OverflowMenu items={moreActions(service)} ariaLabel={`More actions for ${service.name}`} />
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    {/if}

    {#if selectedLogService}
      <section class="service-logs" aria-label="Service logs">
        <div class="service-logs-head">
          <div>
            <h2>{selectedLogService.name} logs</h2>
            <p>{selectedLogs ? `${selectedLogs.tail} line tail` : 'Loading logs'}</p>
            {#if selectedLogs?.exitCode !== null && selectedLogs?.exitCode !== undefined}
              <p>exit {selectedLogs.exitCode}{selectedLogs.exitedAt ? ` at ${selectedLogs.exitedAt}` : ''}{selectedLogs.lastExitReason ? `: ${selectedLogs.lastExitReason}` : ''}</p>
            {/if}
          </div>
          <div class="row-actions">
            <button class="ghost-button compact" type="button" onclick={() => selectedLogService && viewLogs(selectedLogService)}>Refresh logs</button>
            <button class="ghost-button compact" type="button" onclick={() => { selectedLogService = null; selectedLogs = null; }}>Close</button>
          </div>
        </div>
        <pre>{selectedLogs?.text || 'No log output recorded.'}</pre>
        {#if selectedLogs?.events?.length}
          <div class="events-list">
            <h3>Recent events</h3>
            {#each selectedLogs.events as event}
              <div><code>{String(event.at ?? '')}</code> {String(event.type ?? '')} {String(event.status ?? '')}</div>
            {/each}
          </div>
        {/if}
      </section>
    {/if}

    {#if editService}
      <section class="service-editor" aria-label="Edit service">
        <div class="editor-head">
          <h2>Edit {editService.name}</h2>
          <button class="ghost-button compact" type="button" onclick={() => editService = null}>Close</button>
        </div>
        <div class="editor-grid">
          <label><span>Name</span><input bind:value={editForm.name} /></label>
          <label><span>Scope</span><input bind:value={editForm.scope} /></label>
          <label><span>Class</span><select bind:value={editForm.serviceClass}><option value="preview">Preview</option><option value="application">Application</option><option value="infrastructure">Infrastructure</option></select></label>
          <label><span>Trust</span><select bind:value={editForm.trustLevel}><option value="generated">Generated</option><option value="trusted">Trusted</option><option value="external">External</option></select></label>
          <label><span>Ownership</span><select bind:value={editForm.ownership}><option value="static">Static</option><option value="car_managed">CAR-managed</option><option value="external">External</option></select></label>
          {#if editService.kind === 'managed_command'}
            <label class="wide"><span>Command</span><input bind:value={editForm.command} /></label>
            <label><span>CWD</span><input bind:value={editForm.cwd} /></label>
            <label><span>Port</span><input bind:value={editForm.port} /></label>
            <label><span>Health path</span><input bind:value={editForm.healthPath} /></label>
            <label><span>Env policy</span><select bind:value={editForm.envPolicy}><option value="minimal">Minimal</option><option value="allowlist">Allowlist</option><option value="inherit_all">Inherit all</option></select></label>
            <label class="wide"><span>Env overrides</span><textarea bind:value={editForm.env} rows="2" placeholder="Leave blank to keep existing overrides"></textarea></label>
          {/if}
          <label class="check-row"><input type="checkbox" bind:checked={editForm.autostart} /> <span>Autostart on hub start</span></label>
        </div>
        <button class="ghost-button" type="button" onclick={saveEdit} disabled={actionBusy(editService, 'edit')}>Save changes</button>
      </section>
    {/if}
  {/if}
</section>

<style>
  .services-page {
    gap: var(--space-4);
  }

  .service-logs-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .service-logs h2 {
    margin: 0;
    font-size: var(--font-size-3);
    color: var(--color-ink);
  }

  .service-logs-head p {
    margin: var(--space-1) 0 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  .service-logs,
  .service-editor {
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }

  .class-chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .class-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 30px;
    padding: 0 var(--space-2) 0 var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-3);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    font-weight: 550;
    transition:
      background-color var(--transition-fast),
      color var(--transition-fast),
      border-color var(--transition-fast);
  }

  .class-chip:hover {
    color: var(--color-ink);
    border-color: var(--color-border);
  }

  .class-chip .class-chip-count {
    min-width: 18px;
    padding: 0 5px;
    border-radius: var(--radius-2);
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }

  .class-chip.active {
    color: var(--color-ink);
    border-color: currentColor;
  }

  .class-chip.class-preview.active {
    color: var(--color-accent);
    background: var(--color-accent-soft);
  }

  .class-chip.class-application.active {
    color: var(--color-success);
    background: var(--color-success-soft);
  }

  .class-chip.class-infrastructure.active {
    color: var(--color-warning);
    background: var(--color-warning-soft);
  }

  .class-chip.active .class-chip-count {
    background: color-mix(in srgb, currentColor 18%, transparent);
    color: currentColor;
  }

  .services-filters {
    display: grid;
    grid-template-columns: minmax(220px, 1.3fr) minmax(140px, 0.8fr) minmax(160px, 0.8fr) minmax(180px, 1fr) auto;
    gap: var(--space-2);
    align-items: end;
  }

  .service-editor {
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }

  .editor-head {
    display: flex;
    justify-content: space-between;
    gap: var(--space-3);
    align-items: center;
  }

  .editor-head-copy {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .editor-head h2,
  .events-list h3 {
    margin: 0;
    font-size: var(--font-size-3);
    color: var(--color-ink);
  }

  .editor-sub {
    margin: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink-muted);
  }

  .editor-hint {
    margin: 0;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
  }

  .mode-tabs {
    display: inline-flex;
    flex-wrap: nowrap;
    flex-shrink: 0;
    gap: 2px;
    padding: 3px;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-3);
    background: var(--color-surface);
  }

  .mode-tabs button {
    min-height: 28px;
    border: 1px solid transparent;
    border-radius: var(--radius-2);
    background: transparent;
    color: var(--color-ink-muted);
    padding: 0 var(--space-3);
    font-size: var(--font-size-1);
    transition:
      background-color var(--transition-fast),
      color var(--transition-fast);
  }

  .mode-tabs button:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .mode-tabs button.active {
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px var(--color-border-subtle);
  }

  .editor-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-2);
    align-items: end;
  }

  .editor-grid label {
    display: grid;
    gap: var(--space-1);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .editor-grid label.wide {
    grid-column: span 2;
  }

  .editor-grid .check-row {
    grid-template-columns: auto 1fr;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-ink);
  }

  .editor-grid .check-row input[type='checkbox'] {
    width: auto;
    min-height: 0;
    margin: 0;
    padding: 0;
  }

  .services-filters label {
    display: grid;
    gap: var(--space-1);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .services-filters input,
  .services-filters select,
  .editor-grid input,
  .editor-grid select,
  .editor-grid textarea {
    width: 100%;
    min-height: 36px;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    padding: 0 var(--space-3);
    font: inherit;
  }

  .editor-grid textarea {
    min-height: 64px;
    padding: var(--space-2) var(--space-3);
    resize: vertical;
  }

  .services-table-wrap {
    overflow-x: auto;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }

  .services-table {
    width: 100%;
    min-width: 920px;
    border-collapse: collapse;
    font-size: var(--font-size-1);
  }

  .services-table th,
  .services-table td {
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--color-border-subtle);
    text-align: left;
    vertical-align: middle;
  }

  .services-table .col-pid,
  .services-table .col-links,
  .services-table .col-actions {
    text-align: right;
    white-space: nowrap;
    width: 1%;
  }

  .services-table tbody tr {
    transition: background-color var(--transition-fast);
  }

  .services-table tbody tr:hover {
    background: var(--color-surface-muted);
  }

  .services-table th {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0;
    background: var(--color-surface-muted);
  }

  .services-table tr:last-child td {
    border-bottom: 0;
  }

  .services-table tr.attention td {
    background: color-mix(in srgb, var(--color-danger-soft) 38%, transparent);
  }

  .service-name {
    display: grid;
    gap: 3px;
    min-width: 200px;
    max-width: 320px;
  }

  .service-name strong {
    font-size: var(--font-size-2);
    font-weight: 600;
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .service-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.4;
  }

  .service-meta .meta-dot {
    color: var(--color-ink-faint);
    opacity: 0.7;
  }

  .truncate {
    overflow-wrap: anywhere;
  }

  .id-tag {
    font-family: var(--font-mono);
    color: var(--color-ink-muted);
  }

  .cell-muted {
    color: var(--color-ink-muted);
  }

  .cell-num {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    color: var(--color-ink-soft);
  }

  /* Service status badges: read-only soft pills, semantic fill + solid text. */
  .svc-status {
    display: inline-flex;
    align-items: center;
    text-transform: capitalize;
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
  }

  .badge-stack {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    min-width: 140px;
  }

  .meta-badge {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    padding: 0 6px;
    border-radius: var(--radius-2);
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font-size: 10px;
    font-weight: 550;
    letter-spacing: 0.02em;
    white-space: nowrap;
  }

  .class-preview {
    background: var(--color-accent-soft);
    color: var(--color-accent);
  }

  .class-application {
    background: var(--color-success-soft);
    color: var(--color-success);
  }

  .class-infrastructure {
    background: var(--color-warning-soft);
    color: var(--color-warning);
  }

  .svc-status.status-running,
  .svc-status.status-healthy {
    color: var(--color-success);
    background: var(--color-success-soft);
  }

  .svc-status.status-starting {
    color: var(--color-warning);
    background: var(--color-warning-soft);
  }

  .svc-status.status-unhealthy,
  .svc-status.status-failed,
  .svc-status.status-orphaned,
  .svc-status.status-conflict {
    color: var(--color-danger);
    background: var(--color-danger-soft);
  }

  .link-actions,
  .row-actions {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--space-1);
  }

  .link-actions {
    justify-content: flex-end;
  }

  .row-actions {
    justify-content: flex-end;
  }

  :global(.ghost-button.compact) {
    min-height: 28px;
    padding: 0 var(--space-2);
    font-size: var(--font-size-0);
    white-space: nowrap;
  }

  .service-logs {
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }

  .service-logs pre {
    margin: 0;
    max-height: 360px;
    overflow: auto;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface-sunken);
    color: var(--color-ink);
    padding: var(--space-3);
    font-size: var(--font-size-1);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .events-list {
    display: grid;
    gap: var(--space-2);
    color: var(--color-ink);
    font-size: var(--font-size-0);
  }

  @media (max-width: 980px) {
    .services-metrics,
    .services-filters,
    .editor-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .services-filters .ghost-button {
      grid-column: span 2;
    }
  }

  @media (max-width: 640px) {
    .services-hero,
    .service-logs-head {
      flex-direction: column;
    }

    .services-metrics,
    .services-filters,
    .editor-grid {
      grid-template-columns: 1fr;
    }

    .editor-grid label.wide {
      grid-column: auto;
    }

    .services-filters .ghost-button {
      grid-column: auto;
    }
  }
</style>
