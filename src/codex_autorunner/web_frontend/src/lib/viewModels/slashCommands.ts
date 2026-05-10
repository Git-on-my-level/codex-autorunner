export type SlashCommandId =
  | 'help'
  | 'pma'
  | 'status'
  | 'new'
  | 'newt'
  | 'agent'
  | 'model'
  | 'reasoning'
  | 'profile'
  | 'compact'
  | 'reset'
  | 'resume'
  | 'interrupt'
  | 'archive'
  | 'tickets'
  | 'contextspace'
  | 'files'
  | 'queue'
  | 'cancel'
  | 'clearqueue';

export type SlashCommandSpec = {
  id: SlashCommandId;
  name: string;
  aliases?: string[];
  title: string;
  description: string;
  usage: string;
  group: 'Chat' | 'Runtime' | 'Session' | 'Queue' | 'Settings';
  requiresChat?: boolean;
  requiresScopedWorkspace?: boolean;
  requiresRunningTurn?: boolean;
  requiresQueuedTurn?: boolean;
  destructive?: boolean;
};

export type SlashCommandContext = {
  hasActiveChat: boolean;
  hasScopedWorkspace: boolean;
  isRunning: boolean;
  queueDepth: number;
};

export type ParsedSlashCommand = {
  name: string;
  args: string;
  raw: string;
  exact: boolean;
  spec: SlashCommandSpec | null;
};

export type SlashCommandSuggestion = {
  spec: SlashCommandSpec;
  disabledReason: string | null;
  score: number;
};

export const WEB_SLASH_COMMANDS: SlashCommandSpec[] = [
  {
    id: 'help',
    name: 'help',
    aliases: ['commands'],
    title: 'Show commands',
    description: 'Open the web slash command reference.',
    usage: '/help',
    group: 'Chat'
  },
  {
    id: 'pma',
    name: 'pma',
    title: 'PMA status',
    description: 'Show PMA web chat state. Web chat is always PMA-backed.',
    usage: '/pma [status|on|off]',
    group: 'Chat'
  },
  {
    id: 'status',
    name: 'status',
    title: 'Refresh status',
    description: 'Refresh this chat, queue, and runtime status.',
    usage: '/status',
    group: 'Chat',
    requiresChat: true
  },
  {
    id: 'new',
    name: 'new',
    title: 'New chat',
    description: 'Create a new chat in the selected scope.',
    usage: '/new [chat|agent|newt]',
    group: 'Chat'
  },
  {
    id: 'newt',
    name: 'newt',
    title: 'New coding chat',
    description: 'Create a fresh repo/worktree coding chat and switch to it.',
    usage: '/newt',
    group: 'Chat',
    requiresScopedWorkspace: true
  },
  {
    id: 'agent',
    name: 'agent',
    title: 'Set agent',
    description: 'Switch the selected composer agent.',
    usage: '/agent <agent-id>',
    group: 'Settings'
  },
  {
    id: 'model',
    name: 'model',
    title: 'Set model',
    description: 'Set or clear the model override for this composer.',
    usage: '/model <model-id|default>',
    group: 'Settings'
  },
  {
    id: 'reasoning',
    name: 'reasoning',
    aliases: ['effort'],
    title: 'Set reasoning',
    description: 'Set or clear the reasoning effort.',
    usage: '/reasoning <effort|default>',
    group: 'Settings'
  },
  {
    id: 'profile',
    name: 'profile',
    title: 'Set profile',
    description: 'Set or clear the active Hermes profile.',
    usage: '/profile <profile-id|default>',
    group: 'Settings'
  },
  {
    id: 'compact',
    name: 'compact',
    title: 'Compact thread',
    description: 'Generate or save a summary as the next thread seed.',
    usage: '/compact [summary]',
    group: 'Session',
    requiresChat: true
  },
  {
    id: 'reset',
    name: 'reset',
    title: 'Fresh chat state',
    description: 'Create a fresh replacement chat in the same scope.',
    usage: '/reset',
    group: 'Session',
    requiresChat: true,
    destructive: true
  },
  {
    id: 'resume',
    name: 'resume',
    title: 'Resume thread',
    description: 'Reactivate an archived or completed managed thread.',
    usage: '/resume',
    group: 'Session',
    requiresChat: true
  },
  {
    id: 'interrupt',
    name: 'interrupt',
    aliases: ['stop'],
    title: 'Interrupt turn',
    description: 'Stop the currently running turn.',
    usage: '/interrupt',
    group: 'Runtime',
    requiresChat: true,
    requiresRunningTurn: true,
    destructive: true
  },
  {
    id: 'archive',
    name: 'archive',
    title: 'Archive thread',
    description: 'Archive this chat so future work starts elsewhere.',
    usage: '/archive',
    group: 'Session',
    requiresChat: true,
    destructive: true
  },
  {
    id: 'tickets',
    name: 'tickets',
    title: 'Open tickets',
    description: 'Open tickets for the active repo or worktree.',
    usage: '/tickets',
    group: 'Chat',
    requiresChat: true,
    requiresScopedWorkspace: true
  },
  {
    id: 'contextspace',
    name: 'contextspace',
    aliases: ['ctx'],
    title: 'Open contextspace',
    description: 'Open contextspace for the active repo or worktree.',
    usage: '/contextspace',
    group: 'Chat',
    requiresChat: true,
    requiresScopedWorkspace: true
  },
  {
    id: 'files',
    name: 'files',
    title: 'Refresh files',
    description: 'Refresh PMA inbox and outbox artifacts.',
    usage: '/files',
    group: 'Chat'
  },
  {
    id: 'queue',
    name: 'queue',
    title: 'Refresh queue',
    description: 'Refresh queued turns for this chat.',
    usage: '/queue',
    group: 'Queue',
    requiresChat: true
  },
  {
    id: 'cancel',
    name: 'cancel',
    title: 'Cancel queued turn',
    description: 'Cancel a queued turn by position or managed turn id.',
    usage: '/cancel <position|turn-id>',
    group: 'Queue',
    requiresChat: true,
    requiresQueuedTurn: true,
    destructive: true
  },
  {
    id: 'clearqueue',
    name: 'clearqueue',
    aliases: ['clear-queue'],
    title: 'Clear queue',
    description: 'Cancel every queued turn in this chat.',
    usage: '/clearqueue',
    group: 'Queue',
    requiresChat: true,
    requiresQueuedTurn: true,
    destructive: true
  }
];

const COMMAND_BY_TOKEN = new Map<string, SlashCommandSpec>();
for (const spec of WEB_SLASH_COMMANDS) {
  COMMAND_BY_TOKEN.set(spec.name, spec);
  for (const alias of spec.aliases ?? []) COMMAND_BY_TOKEN.set(alias, spec);
}

export function parseSlashCommand(text: string): ParsedSlashCommand | null {
  if (!text.startsWith('/')) return null;
  const raw = text;
  const withoutSlash = text.slice(1);
  const firstWhitespace = withoutSlash.search(/\s/);
  const name = (firstWhitespace < 0 ? withoutSlash : withoutSlash.slice(0, firstWhitespace)).trim().toLowerCase();
  if (!name) {
    return { name: '', args: '', raw, exact: false, spec: null };
  }
  const args = firstWhitespace < 0 ? '' : withoutSlash.slice(firstWhitespace + 1).trim();
  const spec = COMMAND_BY_TOKEN.get(name) ?? null;
  return { name, args, raw, exact: Boolean(spec && spec.name === name), spec };
}

export function slashCommandDisabledReason(
  spec: SlashCommandSpec,
  context: SlashCommandContext
): string | null {
  if (spec.requiresChat && !context.hasActiveChat) return 'Select a chat first';
  if (spec.requiresScopedWorkspace && !context.hasScopedWorkspace) return 'Select a repo or worktree chat first';
  if (spec.requiresRunningTurn && !context.isRunning) return 'No running turn';
  if (spec.requiresQueuedTurn && context.queueDepth <= 0) return 'Queue is empty';
  return null;
}

export function buildSlashCommandSuggestions(
  draft: string,
  context: SlashCommandContext,
  limit = 8
): SlashCommandSuggestion[] {
  if (!draft.startsWith('/')) return [];
  const parsed = parseSlashCommand(draft);
  const query = (parsed?.name ?? '').toLowerCase();
  const hasCommandAndArgs = /\s/.test(draft.slice(1));
  if (hasCommandAndArgs && parsed?.spec) {
    return [
      {
        spec: parsed.spec,
        disabledReason: slashCommandDisabledReason(parsed.spec, context),
        score: 0
      }
    ];
  }
  return WEB_SLASH_COMMANDS.map((spec) => ({
    spec,
    disabledReason: slashCommandDisabledReason(spec, context),
    score: commandMatchScore(spec, query)
  }))
    .filter((item) => item.score < Number.POSITIVE_INFINITY)
    .sort((left, right) => left.score - right.score || left.spec.name.localeCompare(right.spec.name))
    .slice(0, Math.max(1, limit));
}

function commandMatchScore(spec: SlashCommandSpec, query: string): number {
  if (!query) return 10;
  if (spec.name === query) return 0;
  if (spec.name.startsWith(query)) return 1;
  if ((spec.aliases ?? []).some((alias) => alias === query)) return 2;
  if ((spec.aliases ?? []).some((alias) => alias.startsWith(query))) return 3;
  if (spec.title.toLowerCase().includes(query)) return 20;
  if (spec.description.toLowerCase().includes(query)) return 30;
  return Number.POSITIVE_INFINITY;
}
