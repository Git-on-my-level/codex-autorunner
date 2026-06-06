<script lang="ts">
  import { onMount } from 'svelte';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
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

  let model = $state<PreviewServicesReadModel | null>(null);
  let loading = $state(true);
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
  const hasFilters = $derived(Boolean(filters.query.trim() || filters.scope.trim() || filters.status !== 'all' || filters.kind !== 'all'));

  onMount(() => {
    void loadServices();
    return () => {
      if (copyTimer) clearTimeout(copyTimer);
    };
  });

  async function loadServices(): Promise<void> {
    loading = true;
    error = null;
    const result = await webApi.hub.getServicesReadModel();
    loading = false;
    if (!result.ok) {
      error = result.error;
      return;
    }
    model = result.data;
  }

  async function refreshServices(): Promise<void> {
    const result = await webApi.hub.getServicesReadModel();
    if (!result.ok) {
      error = result.error;
      return;
    }
    model = result.data;
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

  const statusOptions: Array<ServiceFilters['status']> = ['all', 'registered', 'starting', 'running', 'healthy', 'unhealthy', 'stopped', 'exited', 'failed', 'orphaned', 'conflict'];
  const kindOptions: Array<ServiceFilters['kind']> = ['all', 'static_file', 'static_dir', 'loopback_url', 'managed_command'];
</script>

<section class="services-page page-stack">
  <div class="services-hero">
    <div>
      <h1>Services</h1>
      <p>Preview service registry, links, process state, and logs.</p>
    </div>
    <button class="ghost-button" type="button" onclick={loadServices} disabled={loading}>
      Refresh
    </button>
  </div>

  {#if loading}
    <ContentSkeleton variant="index" rows={5} />
  {:else if error && !model}
    <div class="state-panel error">
      <strong>Could not load services</strong>
      <p>{error.message}</p>
      <button class="ghost-button" type="button" onclick={loadServices}>Retry</button>
    </div>
  {:else}
    {#if error}
      <div class="state-panel error">
        <strong>Action failed</strong>
        <p>{error.message}</p>
      </div>
    {/if}
    {#if notice}
      <div class="services-notice">{notice}</div>
    {/if}

    <div class="services-metrics" aria-label="Service counts">
      <div><span>Total</span><strong>{counts.total}</strong></div>
      <div><span>Running</span><strong>{counts.running}</strong></div>
      <div class:attention={counts.attention > 0}><span>Attention</span><strong>{counts.attention}</strong></div>
      <div><span>Preview</span><strong>{counts.preview}</strong></div>
      <div><span>Application</span><strong>{counts.application}</strong></div>
      <div><span>Infrastructure</span><strong>{counts.infrastructure}</strong></div>
    </div>

    <section class="service-editor" aria-label="Register service">
      <div class="editor-head">
        <h2>Register</h2>
        <div class="mode-tabs">
          <button class:active={createMode === 'static'} type="button" onclick={() => createMode = 'static'}>Static</button>
          <button class:active={createMode === 'loopback'} type="button" onclick={() => createMode = 'loopback'}>Loopback</button>
          <button class:active={createMode === 'managed'} type="button" onclick={() => createMode = 'managed'}>Managed</button>
        </div>
      </div>
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
      <button class="ghost-button" type="button" onclick={registerService} disabled={Boolean(actionId?.startsWith('create:'))}>Register service</button>
    </section>

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

    {#if services.length === 0}
      <div class="empty-state">
        <strong>No preview services registered</strong>
        <p>Registered static previews, loopback URLs, and managed commands will appear here.</p>
      </div>
    {:else if filteredServices.length === 0}
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
              <th>Substrate</th>
              <th>Scope</th>
              <th>Target</th>
              <th>Uptime</th>
              <th>Owner</th>
              <th>Link</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each filteredServices as service (service.serviceId)}
              {@const eligibility = serviceActionEligibility(service)}
              <tr class:attention={serviceNeedsAttention(service)}>
                <td>
                  <div class="service-name">
                    <strong>{service.name}</strong>
                    <code>{service.serviceId}</code>
                  </div>
                </td>
                <td><span class={`status-pill status-${service.status}`}>{serviceStatusLabel(service.status)}</span></td>
                <td>{healthLabel(service)}</td>
                <td>
                  <div class="badge-stack">
                    <span class={`meta-badge class-${service.serviceClass}`}>{serviceClassLabel(service.serviceClass)}</span>
                    <span class="meta-badge">{serviceTrustLabel(service.trustLevel)}</span>
                    <span class="meta-badge">{serviceOwnershipLabel(service.ownership)}</span>
                  </div>
                </td>
                <td>{serviceKindLabel(service.kind)}</td>
                <td><span class="truncate">{serviceScopeLabel(service)}</span></td>
                <td><span class="truncate">{serviceTargetLabel(service)}</span></td>
                <td>{serviceUptimeLabel(service)}</td>
                <td>{service.ownerPid ? `pid ${service.ownerPid}` : service.createdBy ?? '—'}</td>
                <td>
                  <div class="link-actions">
                    <button class="ghost-button compact" type="button" onclick={() => openCarUrl(service)} disabled={!eligibility.canOpen}>Open</button>
                    <button class="ghost-button compact" type="button" onclick={() => copyCarUrl(service)} disabled={!eligibility.canOpen || actionBusy(service, 'issue-link')}>
                      {copiedServiceId === service.serviceId ? 'Copied' : 'Copy'}
                    </button>
                    <button class="ghost-button compact" type="button" onclick={() => issueLink(service)} disabled={!eligibility.canOpen || actionBusy(service, 'issue-link')}>Issue</button>
                    <button class="ghost-button compact" type="button" onclick={() => revokeLinks(service)} disabled={actionBusy(service, 'revoke-link')}>Revoke</button>
                    {#if issuedLinks[service.serviceId]}
                      <code class="issued-link">{issuedLinks[service.serviceId]}</code>
                    {/if}
                  </div>
                </td>
                <td>
                  <div class="row-actions">
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'start')} disabled={!eligibility.canStart || actionBusy(service, 'start')}>Start</button>
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'stop')} disabled={!eligibility.canStop || actionBusy(service, 'stop')}>Stop</button>
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'restart')} disabled={!eligibility.canRestart || actionBusy(service, 'restart')}>Restart</button>
                    <button class="ghost-button compact" type="button" onclick={() => runLifecycle(service, 'health')} disabled={actionBusy(service, 'health')}>Health</button>
                    <button class="ghost-button compact" type="button" onclick={() => toggleAutostart(service)} disabled={actionBusy(service, 'autostart')}>{service.restartPolicy?.auto_start_on_hub_start === true ? 'Autostart on' : 'Autostart off'}</button>
                    <button class="ghost-button compact" type="button" onclick={() => beginEdit(service)} disabled={service.status === 'running' || service.status === 'healthy' || service.status === 'starting' || service.status === 'unhealthy'}>Edit</button>
                    <button class="ghost-button compact" type="button" onclick={() => viewLogs(service)} disabled={!eligibility.canViewLogs || logsLoadingId === service.serviceId}>Logs</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => runDestructive(service, 'kill')} disabled={!eligibility.canKill || actionBusy(service, 'kill')}>Kill</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => runDestructive(service, 'teardown')} disabled={!eligibility.canTeardown || actionBusy(service, 'teardown')}>Teardown</button>
                    <button class="ghost-button compact danger" type="button" onclick={() => unlinkService(service)} disabled={(!eligibility.canUnlink && !eligibility.requiresForceForUnlink) || actionBusy(service, 'unlink')}>Unlink</button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
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

  .services-hero,
  .service-logs-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .services-hero h1,
  .service-logs h2 {
    margin: 0;
    font-size: var(--font-size-5);
    color: var(--color-ink);
  }

  .services-hero p,
  .service-logs-head p {
    margin: var(--space-1) 0 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
  }

  .services-metrics {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: var(--space-2);
  }

  .services-metrics div,
  .service-logs,
  .service-editor {
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-2);
    background: var(--color-surface);
  }

  .services-metrics div {
    padding: var(--space-3);
    display: grid;
    gap: var(--space-1);
  }

  .services-metrics span {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0;
  }

  .services-metrics strong {
    font-size: var(--font-size-5);
    color: var(--color-ink);
  }

  .services-metrics .attention strong {
    color: var(--color-danger);
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

  .editor-head h2,
  .events-list h3 {
    margin: 0;
    font-size: var(--font-size-3);
    color: var(--color-ink);
  }

  .mode-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .mode-tabs button {
    min-height: 30px;
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-1);
    background: var(--color-surface-muted);
    color: var(--color-ink);
    padding: 0 var(--space-2);
  }

  .mode-tabs button.active {
    border-color: var(--color-accent);
    background: var(--color-accent-soft);
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
    color: var(--color-ink);
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
    min-width: 1080px;
    border-collapse: collapse;
    font-size: var(--font-size-1);
  }

  .services-table th,
  .services-table td {
    padding: var(--space-3);
    border-bottom: 1px solid var(--color-border-subtle);
    text-align: left;
    vertical-align: top;
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
    gap: var(--space-1);
    min-width: 180px;
  }

  .service-name code,
  .truncate {
    overflow-wrap: anywhere;
  }

  .service-name code {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 0 var(--space-2);
    border-radius: 999px;
    background: var(--color-surface-muted);
    color: var(--color-ink);
    text-transform: capitalize;
    white-space: nowrap;
  }

  .badge-stack {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    min-width: 150px;
  }

  .meta-badge {
    display: inline-flex;
    align-items: center;
    min-height: 22px;
    padding: 0 var(--space-2);
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-1);
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-size: var(--font-size-0);
    white-space: nowrap;
  }

  .class-preview {
    border-color: var(--color-accent-soft);
  }

  .class-application {
    border-color: var(--color-success-soft);
  }

  .class-infrastructure {
    border-color: var(--color-warning-soft);
  }

  .status-running,
  .status-healthy {
    color: var(--color-success);
    background: var(--color-success-soft);
  }

  .status-unhealthy,
  .status-failed,
  .status-orphaned,
  .status-conflict {
    color: var(--color-danger);
    background: var(--color-danger-soft);
  }

  .link-actions,
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .issued-link {
    display: block;
    max-width: 220px;
    overflow-wrap: anywhere;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  :global(.ghost-button.compact) {
    min-height: 30px;
    padding: 0 var(--space-2);
    font-size: var(--font-size-0);
  }

  .services-notice {
    border: 1px solid var(--color-success-soft);
    border-radius: var(--radius-2);
    background: color-mix(in srgb, var(--color-success-soft) 70%, transparent);
    color: var(--color-ink);
    padding: var(--space-3);
    font-size: var(--font-size-1);
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
