import { describe, expect, it } from 'vitest';
import type { PreviewServiceReadModel } from '$lib/api/readModelContracts';
import {
  defaultServiceFilters,
  filterServices,
  serviceActionEligibility,
  serviceScopeLabel,
  serviceTargetLabel,
  serviceUptimeLabel
} from './services';

function service(overrides: Partial<PreviewServiceReadModel>): PreviewServiceReadModel {
  return {
    serviceId: 'svc_base123',
    name: 'Base service',
    kind: 'loopback_url',
    status: 'registered',
    createdBy: 'test',
    createdAt: '2026-06-05T00:00:00Z',
    updatedAt: '2026-06-05T00:00:00Z',
    scopeLinks: [],
    scope: null,
    carUrl: '/preview/services/svc_base123/',
    previewUrl: null,
    previewUrlExpiresAt: null,
    proxyEnabled: true,
    directUrl: null,
    host: null,
    port: null,
    ownerPid: null,
    healthCheck: null,
    restartPolicy: {},
    logs: null,
    metadata: {},
    raw: {},
    ...overrides
  };
}

describe('preview service view models', () => {
  it('filters by status, kind, scope, and query', () => {
    const services = [
      service({
        serviceId: 'svc_frontend1',
        name: 'Frontend',
        kind: 'managed_command',
        status: 'healthy',
        scope: 'repo:car',
        carUrl: '/preview/services/svc_frontend1/'
      }),
      service({
        serviceId: 'svc_docs123',
        name: 'Docs',
        kind: 'static_dir',
        status: 'registered',
        scope: 'workspace:/tmp/docs'
      })
    ];

    expect(filterServices(services, { ...defaultServiceFilters(), status: 'healthy' })).toEqual([services[0]]);
    expect(filterServices(services, { ...defaultServiceFilters(), kind: 'static_dir' })).toEqual([services[1]]);
    expect(filterServices(services, { ...defaultServiceFilters(), scope: 'repo:car' })).toEqual([services[0]]);
    expect(filterServices(services, { ...defaultServiceFilters(), query: 'front' })).toEqual([services[0]]);
  });

  it('derives action eligibility from kind and running state', () => {
    const runningManaged = service({ kind: 'managed_command', status: 'healthy', logs: { path: 'log' } });
    const stoppedManaged = service({ kind: 'managed_command', status: 'stopped' });
    const loopback = service({ kind: 'loopback_url', status: 'healthy' });

    expect(serviceActionEligibility(runningManaged)).toMatchObject({
      canStart: false,
      canStop: true,
      canRestart: true,
      canKill: true,
      canUnlink: false,
      canViewLogs: true
    });
    expect(serviceActionEligibility(stoppedManaged)).toMatchObject({
      canStart: true,
      canStop: false,
      canUnlink: true
    });
    expect(serviceActionEligibility(loopback)).toMatchObject({
      canStart: false,
      canStop: false,
      canRestart: false,
      canKill: false,
      canUnlink: false
    });
  });

  it('formats scope, target, and uptime labels', () => {
    const managed = service({
      status: 'running',
      host: '127.0.0.1',
      port: 39001,
      scopeLinks: [{ kind: 'repo', id: 'car' }],
      raw: { process: { started_at: '2026-06-05T00:00:00Z' } }
    });

    expect(serviceScopeLabel(managed)).toBe('repo:car');
    expect(serviceTargetLabel(managed)).toBe('127.0.0.1:39001');
    expect(serviceUptimeLabel(managed, Date.parse('2026-06-05T01:02:00Z'))).toBe('1h 2m');
  });
});
