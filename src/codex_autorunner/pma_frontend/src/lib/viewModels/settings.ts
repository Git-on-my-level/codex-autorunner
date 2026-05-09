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
  modelCatalogs?: Record<string, JsonRecord[] | null>;
  voiceConfig?: JsonRecord | null;
};

export function buildSettingsViewModel(input: SettingsBuildInput): SettingsViewModel {
  const agents = input.agents ?? [];
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
        value: agents.length > 0 ? `${agents.length} agents detected` : 'No agents detected',
        tone: agents.length > 0 ? 'ok' : 'warning'
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
    autorunner_model_override: blankToNull(session.modelOverride),
    autorunner_effort_override: blankToNull(session.effortOverride),
    autorunner_approval_policy: blankToNull(session.approvalPolicy),
    autorunner_sandbox_mode: blankToNull(session.sandboxMode),
    autorunner_workspace_write_network: session.workspaceWriteNetwork,
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
  const modelGate = capabilityGate(agent, 'list_models');
  const modelStatus = modelGate.allowed ? (models ? 'available' : 'unavailable') : 'unsupported';
  const modelCount = models?.length ?? 0;
  return {
    id,
    name: stringValue(agent.name) || id,
    capabilities,
    modelStatus,
    modelCount,
    modelLabel:
      modelStatus === 'available'
        ? `${modelCount} models`
        : modelStatus === 'unsupported'
          ? modelGate.reason || 'Model listing unsupported'
          : 'Model listing unavailable',
    providerLabel: providerLabel(agent)
  };
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

function providerLabel(agent: JsonRecord): string {
  const profile = stringValue(agent.default_profile ?? agent.metadata_profile);
  const version = stringValue(agent.version ?? agent.protocol_version);
  if (profile && version) return `${profile} · ${version}`;
  if (profile) return profile;
  if (version) return version;
  return 'Configured locally';
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
