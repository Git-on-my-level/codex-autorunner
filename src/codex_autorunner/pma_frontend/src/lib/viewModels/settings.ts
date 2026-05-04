import type { SensitiveApprovalRequest, SurfaceArtifact } from './domain';
import { filterSensitiveCarApprovals } from './pmaChat';

type JsonRecord = Record<string, unknown>;

export type SettingsSessionState = {
  modelOverride: string;
  effortOverride: string;
  stopAfterRuns: string;
  approvalPolicy: string;
  sandboxMode: string;
  workspaceWriteNetwork: boolean | null;
};

export type SettingsAgentStatus = {
  id: string;
  name: string;
  capabilities: string[];
  role: 'pma' | 'coding';
  modelStatus: 'available' | 'unsupported' | 'unavailable';
  modelCount: number;
  modelLabel: string;
  providerLabel: string;
};

export type SettingsStatusItem = {
  label: string;
  value: string;
  tone: 'ok' | 'warning' | 'muted';
};

export type SettingsSensitiveAction = {
  id: string;
  label: string;
  description: string;
  available: boolean;
  reason: string;
};

export type SettingsViewModel = {
  hub: SettingsStatusItem[];
  session: SettingsSessionState;
  pmaAgents: SettingsAgentStatus[];
  codingAgents: SettingsAgentStatus[];
  integrations: SettingsStatusItem[];
  filebox: SettingsStatusItem[];
  secrets: SettingsStatusItem[];
  sensitiveActions: SettingsSensitiveAction[];
  approvals: SensitiveApprovalRequest[];
  advanced: SettingsStatusItem[];
};

export type SettingsBuildInput = {
  session?: JsonRecord | null;
  agents?: JsonRecord[];
  modelCatalogs?: Record<string, JsonRecord[] | null>;
  fileArtifacts?: SurfaceArtifact[];
  approvals?: SensitiveApprovalRequest[];
};

const sensitiveActionCopy: SettingsSensitiveAction[] = [
  {
    id: 'modify-car-config',
    label: 'Modify CAR config',
    description: 'Change persistent hub or repo CAR configuration.',
    available: false,
    reason: 'No wired PMA settings route is exposed yet.'
  },
  {
    id: 'manage-secrets',
    label: 'Manage secrets',
    description: 'Add, rotate, or remove stored secret values.',
    available: false,
    reason: 'Secret management is not exposed in the PMA settings API yet.'
  },
  {
    id: 'delete-repo-state',
    label: 'Delete repo/worktree state',
    description: 'Remove repo registrations, worktrees, or local workspace state.',
    available: false,
    reason: 'Destructive state deletion is intentionally hidden until a dedicated route exists.'
  },
  {
    id: 'destructive-cleanup',
    label: 'Destructive cleanup',
    description: 'Delete generated runtime files, snapshots, or cached state.',
    available: false,
    reason: 'Cleanup is unavailable from this simplified settings page.'
  },
  {
    id: 'reset-hub-runtime',
    label: 'Reset hub/runtime state',
    description: 'Clear runtime state or reset the hub control plane.',
    available: false,
    reason: 'Runtime reset is not wired in the PMA settings API.'
  },
  {
    id: 'change-credentials',
    label: 'Change stored credentials',
    description: 'Update provider tokens, app credentials, or account-linked secrets.',
    available: false,
    reason: 'Credential storage is not exposed through a safe settings route yet.'
  }
];

export function buildSettingsViewModel(input: SettingsBuildInput): SettingsViewModel {
  const agents = input.agents ?? [];
  const modelCatalogs = input.modelCatalogs ?? {};
  const agentStatuses = agents.map((agent) => mapAgentStatus(agent, modelCatalogs));
  const pmaAgents = agentStatuses.filter((agent) => agent.role === 'pma');
  const codingAgents = agentStatuses.filter((agent) => agent.role === 'coding');
  const fileArtifacts = input.fileArtifacts ?? [];
  const approvals = filterSensitiveCarApprovals(input.approvals ?? []);
  const session = mapSession(input.session ?? {});

  return {
    hub: [
      { label: 'Hub mode', value: 'Local PMA hub', tone: 'ok' },
      {
        label: 'Runtime settings API',
        value: input.session ? 'Available' : 'Unavailable',
        tone: input.session ? 'ok' : 'warning'
      },
      {
        label: 'Sensitive approvals',
        value: approvals.length > 0 ? `${approvals.length} pending` : 'No pending sensitive approvals',
        tone: approvals.length > 0 ? 'warning' : 'ok'
      }
    ],
    session,
    pmaAgents,
    codingAgents,
    integrations: [
      {
        label: 'Agent capability discovery',
        value: agents.length > 0 ? `${agents.length} agents detected` : 'No agents detected',
        tone: agents.length > 0 ? 'ok' : 'warning'
      },
      {
        label: 'External integrations',
        value: integrationLabel(agents),
        tone: agents.some((agent) => stringValue(agent.id).includes('opencode') || stringValue(agent.id).includes('hermes'))
          ? 'ok'
          : 'muted'
      }
    ],
    filebox: [
      {
        label: 'Attachments API',
        value: fileArtifacts.length > 0 ? `${fileArtifacts.length} surfaced files` : 'Available, no files surfaced',
        tone: 'ok'
      },
      {
        label: 'Supported inputs',
        value: 'Files, images, links',
        tone: 'ok'
      }
    ],
    secrets: [
      {
        label: 'Secret management',
        value: 'Unavailable in PMA settings',
        tone: 'muted'
      },
      {
        label: 'Stored credentials',
        value: 'Hidden until a safe route exists',
        tone: 'muted'
      }
    ],
    sensitiveActions: sensitiveActionCopy,
    approvals,
    advanced: [
      {
        label: 'Approval policy',
        value: session.approvalPolicy || 'Default',
        tone: 'muted'
      },
      {
        label: 'Sandbox mode',
        value: session.sandboxMode || 'Default',
        tone: 'muted'
      },
      {
        label: 'Workspace-write network',
        value: session.workspaceWriteNetwork === null ? 'Default' : session.workspaceWriteNetwork ? 'Enabled' : 'Disabled',
        tone: 'muted'
      }
    ]
  };
}

export function buildSessionUpdatePayload(session: SettingsSessionState): JsonRecord {
  return {
    autorunner_model_override: blankToNull(session.modelOverride),
    autorunner_effort_override: blankToNull(session.effortOverride),
    runner_stop_after_runs: positiveIntegerOrNull(session.stopAfterRuns)
  };
}

function mapSession(raw: JsonRecord): SettingsSessionState {
  return {
    modelOverride: stringValue(raw.autorunner_model_override),
    effortOverride: stringValue(raw.autorunner_effort_override),
    stopAfterRuns: numberString(raw.runner_stop_after_runs),
    approvalPolicy: stringValue(raw.autorunner_approval_policy),
    sandboxMode: stringValue(raw.autorunner_sandbox_mode),
    workspaceWriteNetwork:
      typeof raw.autorunner_workspace_write_network === 'boolean' ? raw.autorunner_workspace_write_network : null
  };
}

function mapAgentStatus(agent: JsonRecord, modelCatalogs: Record<string, JsonRecord[] | null>): SettingsAgentStatus {
  const id = stringValue(agent.id) || 'agent';
  const capabilities = stringArray(agent.capabilities);
  const models = modelCatalogs[id] ?? null;
  const supportsModelListing = capabilities.includes('model_listing');
  const modelStatus = supportsModelListing ? (models ? 'available' : 'unavailable') : 'unsupported';
  const modelCount = models?.length ?? 0;
  return {
    id,
    name: stringValue(agent.name) || id,
    capabilities,
    role: isPmaAgent(id, capabilities) ? 'pma' : 'coding',
    modelStatus,
    modelCount,
    modelLabel:
      modelStatus === 'available'
        ? `${modelCount} models`
        : modelStatus === 'unsupported'
          ? 'Model listing unsupported'
          : 'Model listing unavailable',
    providerLabel: providerLabel(agent)
  };
}

function isPmaAgent(id: string, capabilities: string[]): boolean {
  return id === 'hermes' || capabilities.includes('managed_threads') || capabilities.includes('durable_threads');
}

function providerLabel(agent: JsonRecord): string {
  const profile = stringValue(agent.default_profile ?? agent.metadata_profile);
  const version = stringValue(agent.version ?? agent.protocol_version);
  if (profile && version) return `${profile} · ${version}`;
  if (profile) return profile;
  if (version) return version;
  return 'Configured locally';
}

function integrationLabel(agents: JsonRecord[]): string {
  const ids = agents.map((agent) => stringValue(agent.id)).filter(Boolean);
  const visible = ids.filter((id) => ['hermes', 'opencode', 'codex'].includes(id));
  return visible.length > 0 ? visible.join(', ') : 'No integration status exposed';
}

function stringValue(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

function numberString(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : stringValue(value);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [];
}

function blankToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function positiveIntegerOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}
