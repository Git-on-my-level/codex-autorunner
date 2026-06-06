import type {
  PreviewServiceKind,
  PreviewServiceReadModel,
  PreviewServiceStatus,
  PreviewServicesCounts,
  PreviewServicesReadModel
} from '$lib/api/readModelContracts';

export type ServiceStatusFilter = 'all' | PreviewServiceStatus;
export type ServiceKindFilter = 'all' | PreviewServiceKind;
export type ServiceClassFilter = 'all' | PreviewServiceReadModel['serviceClass'];

export type ServiceFilters = {
  query: string;
  status: ServiceStatusFilter;
  kind: ServiceKindFilter;
  serviceClass: ServiceClassFilter;
  scope: string;
};

export type ServiceActionEligibility = {
  canOpen: boolean;
  canStart: boolean;
  canStop: boolean;
  canRestart: boolean;
  canKill: boolean;
  canTeardown: boolean;
  canUnlink: boolean;
  requiresForceForUnlink: boolean;
  canViewLogs: boolean;
};

const runningStatuses = new Set<PreviewServiceStatus>(['starting', 'running', 'healthy', 'unhealthy']);
const attentionStatuses = new Set<PreviewServiceStatus>([
  'unhealthy',
  'failed',
  'orphaned',
  'conflict',
  'exited'
]);

export function defaultServiceFilters(): ServiceFilters {
  return { query: '', status: 'all', kind: 'all', serviceClass: 'all', scope: '' };
}

export function filterServices(
  services: PreviewServiceReadModel[],
  filters: ServiceFilters
): PreviewServiceReadModel[] {
  const query = filters.query.trim().toLowerCase();
  const scope = filters.scope.trim().toLowerCase();
  return services.filter((service) => {
    if (filters.status !== 'all' && service.status !== filters.status) return false;
    if (filters.kind !== 'all' && service.kind !== filters.kind) return false;
    if (filters.serviceClass !== 'all' && service.serviceClass !== filters.serviceClass) return false;
    if (scope && !(service.scope ?? '').toLowerCase().includes(scope)) return false;
    if (!query) return true;
    return [
      service.name,
      service.serviceId,
      service.previewUrl ?? '',
      service.carUrl,
      service.directUrl ?? '',
      service.scope ?? ''
    ]
      .join(' ')
      .toLowerCase()
      .includes(query);
  });
}

export function serviceActionEligibility(service: PreviewServiceReadModel): ServiceActionEligibility {
  if (Object.keys(service.capabilities).length > 0) {
    return {
      canOpen: service.capabilities.can_open === true,
      canStart: service.capabilities.can_start === true,
      canStop: service.capabilities.can_stop === true,
      canRestart: service.capabilities.can_restart === true,
      canKill: service.capabilities.can_kill === true,
      canTeardown: service.capabilities.can_teardown === true,
      canUnlink: service.capabilities.can_unlink === true,
      requiresForceForUnlink: service.capabilities.requires_force_for_unlink === true,
      canViewLogs: service.capabilities.can_view_logs === true
    };
  }
  const managed = service.kind === 'managed_command';
  const running = serviceIsRunning(service);
  return {
    canOpen: service.proxyEnabled && Boolean(serviceOpenUrl(service)),
    canStart: managed && ['stopped', 'exited', 'failed', 'registered', 'conflict'].includes(service.status),
    canStop: managed && running,
    canRestart: managed,
    canKill: managed && running,
    canTeardown: true,
    canUnlink: !running,
    requiresForceForUnlink: managed && running,
    canViewLogs: managed && Boolean(service.logs)
  };
}

export function serviceIsRunning(service: PreviewServiceReadModel): boolean {
  return runningStatuses.has(service.status);
}

export function serviceNeedsAttention(service: PreviewServiceReadModel): boolean {
  return attentionStatuses.has(service.status);
}

export function serviceKindLabel(kind: PreviewServiceKind): string {
  const labels: Record<PreviewServiceKind, string> = {
    static_file: 'Static file',
    static_dir: 'Static dir',
    loopback_url: 'Loopback URL',
    managed_command: 'Managed command'
  };
  return labels[kind] ?? kind;
}

export function serviceClassLabel(serviceClass: PreviewServiceReadModel['serviceClass']): string {
  const labels: Record<PreviewServiceReadModel['serviceClass'], string> = {
    preview: 'Preview',
    application: 'Application',
    infrastructure: 'Infrastructure'
  };
  return labels[serviceClass] ?? serviceClass;
}

export function serviceTrustLabel(trustLevel: PreviewServiceReadModel['trustLevel']): string {
  const labels: Record<PreviewServiceReadModel['trustLevel'], string> = {
    trusted: 'Trusted',
    generated: 'Generated',
    external: 'External'
  };
  return labels[trustLevel] ?? trustLevel;
}

export function serviceOwnershipLabel(ownership: PreviewServiceReadModel['ownership']): string {
  const labels: Record<PreviewServiceReadModel['ownership'], string> = {
    static: 'Static',
    car_managed: 'CAR-managed',
    external: 'External'
  };
  return labels[ownership] ?? ownership;
}

export function serviceStatusLabel(status: PreviewServiceStatus): string {
  return status.replaceAll('_', ' ');
}

export function serviceScopeLabel(service: PreviewServiceReadModel): string {
  if (service.scope) return service.scope;
  if (!service.scopeLinks.length) return 'hub';
  const first = service.scopeLinks[0];
  if (first.id) return `${first.kind}:${first.id}`;
  if (first.path) return `${first.kind}:${first.path}`;
  return first.kind;
}

export function serviceTargetLabel(service: PreviewServiceReadModel): string {
  if (service.port) return `${service.host ?? '127.0.0.1'}:${service.port}`;
  return service.directUrl ?? service.previewUrl ?? service.carUrl;
}

export function serviceOpenUrl(service: PreviewServiceReadModel): string {
  return service.previewUrl ?? service.carUrl;
}

export function serviceUptimeLabel(service: PreviewServiceReadModel, now = Date.now()): string {
  if (!serviceIsRunning(service)) return '—';
  const startedAt = typeof service.raw.process === 'object' && service.raw.process
    ? String((service.raw.process as Record<string, unknown>).started_at ?? '')
    : '';
  const timestamp = startedAt || service.updatedAt || service.createdAt || '';
  const started = Date.parse(timestamp);
  if (!Number.isFinite(started)) return 'running';
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export function serviceCounts(model: PreviewServicesReadModel | null): PreviewServicesCounts {
  return model?.counts ?? {
    total: 0,
    running: 0,
    attention: 0,
    managed: 0,
    static: 0,
    loopback: 0,
    preview: 0,
    application: 0,
    infrastructure: 0
  };
}
