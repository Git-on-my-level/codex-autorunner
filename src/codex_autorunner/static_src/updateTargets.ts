export type UpdateTarget = string;

export interface UpdateTargetOptionResponse {
  value?: string;
  label?: string;
  description?: string | null;
  includes_web?: boolean;
  restart_notice?: string | null;
}

export interface UpdateTargetsResponse {
  targets?: UpdateTargetOptionResponse[];
  default_target?: string;
}

export interface UpdateTargetOption {
  value: UpdateTarget;
  label: string;
  description: string;
  includesWeb: boolean;
  restartNotice: string;
}

const DEFAULT_UPDATE_TARGET = "all";
const UPDATE_TARGET_ALIASES = new Map<string, string>([
  ["all", "all"],
  ["both", "all"],
  ["web", "web"],
  ["hub", "web"],
  ["server", "web"],
  ["ui", "web"],
  ["chat", "chat"],
  ["chat-apps", "chat"],
  ["apps", "chat"],
  ["telegram", "telegram"],
  ["tg", "telegram"],
  ["bot", "telegram"],
  ["discord", "discord"],
  ["dc", "discord"],
]);
const updateTargetRegistry = new Map<UpdateTarget, UpdateTargetOption>();

function titleCaseTarget(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function fallbackTargetOption(value: UpdateTarget): UpdateTargetOption {
  const normalized = normalizeUpdateTarget(value);
  return updateTargetRegistry.get(normalized) || {
    value: normalized,
    label: titleCaseTarget(normalized),
    description: titleCaseTarget(normalized),
    includesWeb: normalized === "all" || normalized === "web",
    restartNotice: "The selected services will restart.",
  };
}

export function normalizeUpdateTarget(
  value: unknown,
  fallback: UpdateTarget = DEFAULT_UPDATE_TARGET,
): UpdateTarget {
  if (typeof value !== "string") return fallback;
  const normalized = value.trim().toLowerCase();
  if (!normalized) return fallback;
  return UPDATE_TARGET_ALIASES.get(normalized) || normalized;
}

export function getUpdateTarget(selectId: string | null): UpdateTarget {
  const select = selectId ? document.getElementById(selectId) as HTMLSelectElement | null : null;
  return normalizeUpdateTarget(select ? select.value : DEFAULT_UPDATE_TARGET);
}

export function describeUpdateTarget(target: UpdateTarget): string {
  return fallbackTargetOption(target).label;
}

export function includesWebUpdateTarget(target: UpdateTarget): boolean {
  return fallbackTargetOption(target).includesWeb;
}

export function updateRestartNotice(target: UpdateTarget): string {
  return fallbackTargetOption(target).restartNotice;
}

export function updateTargetOptionsFromResponse(
  payload: UpdateTargetsResponse | null | undefined,
): { options: UpdateTargetOption[]; defaultTarget: UpdateTarget } {
  const rawOptions = Array.isArray(payload?.targets) ? payload.targets : [];
  const options: UpdateTargetOption[] = [];
  const seen = new Set<string>();

  rawOptions.forEach((entry) => {
    const value = normalizeUpdateTarget(entry?.value);
    if (!value || seen.has(value)) return;
    const fallback = fallbackTargetOption(value);
    const option: UpdateTargetOption = {
      value,
      label: typeof entry?.label === "string" && entry.label.trim() ? entry.label.trim() : fallback.label,
      description:
        typeof entry?.description === "string" && entry.description.trim()
          ? entry.description.trim()
          : fallback.description,
      includesWeb: typeof entry?.includes_web === "boolean" ? entry.includes_web : fallback.includesWeb,
      restartNotice:
        typeof entry?.restart_notice === "string" && entry.restart_notice.trim()
          ? entry.restart_notice.trim()
          : fallback.restartNotice,
    };
    seen.add(value);
    updateTargetRegistry.set(value, option);
    options.push(option);
  });

  return {
    options,
    defaultTarget: normalizeUpdateTarget(payload?.default_target, DEFAULT_UPDATE_TARGET),
  };
}
