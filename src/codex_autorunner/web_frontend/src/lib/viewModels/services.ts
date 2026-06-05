import type {
  PreviewServiceKind,
  PreviewServiceReadModel,
  PreviewServiceStatus,
  PreviewServicesCounts,
  PreviewServicesReadModel
} from '$lib/api/readModelContracts';

export type ServiceStatusFilter = 'all' | PreviewServiceStatus;
export type ServiceKindFilter = 'all' | PreviewServiceKind;

export type ServiceFilters = {
  query: string;
  status: ServiceStatusFilter;
  kind: ServiceKindFilter;
  scope: string;
};

export type ServiceActionEligibility = {
  canStart: boolean;
  canStop: boolean;
  canRestart: boolean;
  canKill: boolean;
  canTeardown: boolean;
  canUnlink: boolean;
  canViewLogs: boolean;
};

const runningStatuses = new Set<PreviewServiceStatus>(['starting', 'running', 'healthy', 'unhealthy']);
const attentionStatuses = new Set<PreviewServiceStatus>(['unhealthy', 'failed', 'orphaned', 'conflict']);

export function defaultServiceFilters(): ServiceFilters {
  return { query: '', status: 'all', kind: 'all', scope: '' };
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
    if (scope && !(service.scope ?? '').toLowerCase().includes(scope)) return false;
    if (!query) return true;
    return [service.name, service.serviceId, service.carUrl, service.directUrl ?? '', service.scope ?? '']
      .join(' ')
      .toLowerCase()
      .includes(query);
  });
}

export function serviceActionEligibility(service: PreviewServiceReadModel): ServiceActionEligibility {
  const managed = service.kind === 'managed_command';
  const running = serviceIsRunning(service);
  return {
    canStart: managed && ['stopped', 'exited', 'failed', 'registered', 'conflict'].includes(service.status),
    canStop: managed && running,
    canRestart: managed,
    canKill: managed && running,
    canTeardown: true,
    canUnlink: !running,
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
  return service.directUrl ?? service.carUrl;
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
  return model?.counts ?? { total: 0, running: 0, attention: 0, managed: 0, static: 0, loopback: 0 };
}
