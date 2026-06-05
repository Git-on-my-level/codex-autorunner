type JsonRecord = Record<string, unknown>;

export type SettingsSessionState = {
  modelOverrides: Record<string, string>;
  effortOverride: string;
  stopAfterRuns: string;
  approvalPolicy: string;
  sandboxMode: string;
  workspaceWriteNetwork: boolean | null;
  ticketFlowRequireCommit: boolean;
};

export type SettingsAgentStatus = {
  id: string;
  name: string;
  capabilities: string[];
  reachable: boolean | null;
  usable: boolean;
  status: string;
  statusLabel: string;
  statusDetail: string;
  modelStatus: 'available' | 'unsupported' | 'unavailable';
  modelCount: number;
  modelLabel: string;
  modelOptions: SettingsModelOption[];
};

export type SettingsModelOption = {
  id: string;
  label: string;
};

export type SettingsStatusItem = {
  label: string;
  value: string;
  tone: 'ok' | 'warning' | 'muted';
};

export type SettingsViewModel = {
  hub: SettingsStatusItem[];
  session: SettingsSessionState;
  agents: SettingsAgentStatus[];
  integrations: SettingsStatusItem[];
  secrets: SettingsStatusItem[];
  advanced: SettingsStatusItem[];
  voice: SettingsVoiceStatus;
};

export type SettingsVoiceStatus = {
  enabled: boolean;
  configured: boolean;
  provider: string;
  apiKeyEnv: string | null;
  hasApiKey: boolean;
  hint: string | null;
  rows: SettingsStatusItem[];
};

export type SettingsBuildInput = {
  session?: JsonRecord | null;
  agents?: JsonRecord[];
  agentStatuses?: JsonRecord[];
  modelCatalogs?: Record<string, JsonRecord[] | null>;
  voiceConfig?: JsonRecord | null;
};

export function buildSettingsViewModel(input: SettingsBuildInput): SettingsViewModel {
  const selectableAgents = input.agents ?? [];
  const agents = input.agentStatuses?.length ? input.agentStatuses : selectableAgents;
  const modelCatalogs = input.modelCatalogs ?? {};
  const agentStatuses = agents.map((agent) => mapAgentStatus(agent, modelCatalogs));
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
        label: 'Settings changes',
        value: 'Direct save',
        tone: 'ok'
      }
    ],
    session,
    agents: agentStatuses,
    integrations: [
      {
        label: 'Agent capability discovery',
        value:
          selectableAgents.length > 0
            ? `${selectableAgents.length} ready agents`
            : agents.length > 0
              ? 'No ready agents'
              : 'No agents configured',
        tone: selectableAgents.length > 0 ? 'ok' : 'warning'
      },
      {
        label: 'Chat setup',
        value: 'Configure Discord, Telegram, and notifications with PMA guidance',
        tone: 'muted'
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
    ],
    voice: mapVoiceStatus(input.voiceConfig ?? null)
  };
}

function mapVoiceStatus(raw: JsonRecord | null): SettingsVoiceStatus {
  const enabled = raw?.enabled === true;
  const provider = stringValue(raw?.provider) || 'local_whisper';
  const apiKeyEnv = stringValue(raw?.api_key_env) || null;
  const hasApiKey = raw?.has_api_key === true;
  const missingExtra = stringValue(raw?.missing_extra);
  const isLocal = provider.startsWith('local') || provider.startsWith('mlx');

  let hint: string | null = null;
  if (!raw) {
    hint = 'Voice config could not be loaded from the hub.';
  } else if (!enabled) {
    if (missingExtra) {
      hint = `Install voice runtime: \`pip install '.[${missingExtra}]'\``;
    } else if (apiKeyEnv && !hasApiKey) {
      hint = `Set ${apiKeyEnv} in your hub environment to enable Whisper transcription.`;
    } else if (isLocal) {
      hint = 'Local Whisper provider is configured but disabled. Enable `voice.enabled: true` in your hub config.';
    } else {
      hint = 'Voice transcription is disabled. Configure a provider in `voice` config.yml.';
    }
  }

  const rows: SettingsStatusItem[] = [
    {
      label: 'Voice transcription',
      value: !raw
        ? 'Status unavailable'
        : enabled
          ? 'Enabled'
          : apiKeyEnv && !hasApiKey
            ? `Disabled · ${apiKeyEnv} not set`
            : 'Disabled',
      tone: !raw ? 'muted' : enabled ? 'ok' : 'warning'
    },
    {
      label: 'Provider',
      value: provider,
      tone: 'muted'
    }
  ];
  if (apiKeyEnv) {
    rows.push({
      label: 'API key env',
      value: hasApiKey ? `${apiKeyEnv} (set)` : `${apiKeyEnv} (unset)`,
      tone: hasApiKey ? 'ok' : 'warning'
    });
  }

  return {
    enabled,
    configured: Boolean(raw),
    provider,
    apiKeyEnv,
    hasApiKey,
    hint,
    rows
  };
}

export function buildSessionUpdatePayload(session: SettingsSessionState): JsonRecord {
  return {
    autorunner_model_overrides: Object.fromEntries(
      Object.entries(session.modelOverrides)
        .map(([agent, model]) => [agent.trim().toLowerCase(), model.trim()])
        .filter(([agent, model]) => agent && model)
    ),
    autorunner_effort_override: blankToNull(session.effortOverride),
    autorunner_approval_policy: blankToNull(session.approvalPolicy),
    autorunner_sandbox_mode: blankToNull(session.sandboxMode),
    autorunner_workspace_write_network: session.workspaceWriteNetwork,
    ticket_flow_require_commit: session.ticketFlowRequireCommit,
    runner_stop_after_runs: positiveIntegerOrNull(session.stopAfterRuns)
  };
}

function mapSession(raw: JsonRecord): SettingsSessionState {
  const modelOverrides = recordValue(raw.autorunner_model_overrides);
  const normalizedModelOverrides = Object.fromEntries(
    Object.entries(modelOverrides)
      .map(([agent, model]) => [agent.trim().toLowerCase(), stringValue(model)])
      .filter(([agent, model]) => agent && model)
  );
  return {
    modelOverrides: normalizedModelOverrides,
    effortOverride: stringValue(raw.autorunner_effort_override),
    stopAfterRuns: numberString(raw.runner_stop_after_runs),
    approvalPolicy: stringValue(raw.autorunner_approval_policy),
    sandboxMode: stringValue(raw.autorunner_sandbox_mode),
    workspaceWriteNetwork:
      typeof raw.autorunner_workspace_write_network === 'boolean' ? raw.autorunner_workspace_write_network : null,
    ticketFlowRequireCommit: typeof raw.ticket_flow_require_commit === 'boolean' ? raw.ticket_flow_require_commit : true
  };
}

function mapAgentStatus(agent: JsonRecord, modelCatalogs: Record<string, JsonRecord[] | null>): SettingsAgentStatus {
  const id = (stringValue(agent.id) || 'agent').toLowerCase();
  const capabilities = stringArray(agent.capabilities);
  const reachable = typeof agent.reachable === 'boolean' ? agent.reachable : null;
  const usable = agent.usable !== false && reachable !== false;
  const status = stringValue(agent.status) || (reachable === null ? 'configured' : usable ? 'ready' : 'offline');
  const statusLabel = stringValue(agent.status_label ?? agent.statusLabel) || (reachable === null ? 'Configured' : usable ? 'Ready' : 'Offline');
  const statusDetail =
    stringValue(agent.status_detail ?? agent.statusDetail) ||
    (reachable === null ? 'This agent is configured; CAR cannot verify live reachability yet.' : usable ? 'Runtime is reachable.' : 'This agent is not reachable right now.');
  const models = modelCatalogs[id] ?? modelCatalogs[stringValue(agent.id)] ?? null;
  const modelGate = capabilityGate(agent, 'list_models');
  const modelStatus = !usable ? 'unavailable' : modelGate.allowed ? (models ? 'available' : 'unavailable') : 'unsupported';
  const modelCount = models?.length ?? 0;
  const modelOptions = modelCatalogOptions(models);
  return {
    id,
    name: stringValue(agent.name) || id,
    capabilities,
    reachable,
    usable,
    status,
    statusLabel,
    statusDetail,
    modelStatus,
    modelCount,
    modelLabel:
      modelStatus === 'available'
        ? `${modelCount} models`
        : modelStatus === 'unsupported'
          ? modelGate.reason || 'Model selection not supported'
          : reachable === null
            ? 'Model selection not verified yet'
            : !usable
              ? 'Agent offline; models unavailable'
            : 'Could not load models',
    modelOptions
  };
}

function modelCatalogOptions(models: JsonRecord[] | null): SettingsModelOption[] {
  if (!models) return [];
  return models
    .map((model) => {
      const id = stringValue(model.id);
      if (!id) return null;
      const displayName = stringValue(model.display_name);
      return {
        id,
        label: displayName && displayName !== id ? `${displayName} (${id})` : id
      };
    })
    .filter((model): model is SettingsModelOption => Boolean(model));
}

function capabilityGate(agent: JsonRecord, action: string): { allowed: boolean; reason: string | null } {
  const projection = recordValue(agent.capability_projection);
  const actions = recordValue(projection.actions);
  const gate = recordValue(actions[action]);
  return {
    allowed: gate.allowed === true,
    reason: stringValue(gate.reason) || null
  };
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

function recordValue(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
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
