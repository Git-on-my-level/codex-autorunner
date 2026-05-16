import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';

export type PmaChatArchitecturePrinciple =
  | 'backendTranscriptProjection'
  | 'thinRouteSurface'
  | 'applicationCommandBoundary'
  | 'capabilityAdapters'
  | 'deterministicRepair'
  | 'windowedRendering'
  | 'contractTests';

export type PmaChatArchitectureSignal = {
  principle: PmaChatArchitecturePrinciple;
  satisfied: boolean;
  weight: number;
  detail: string;
};

export type PmaChatArchitectureGoalEvaluation = {
  score: number;
  targetScore: number;
  satisfied: boolean;
  strengths: string[];
  gaps: string[];
  signals: PmaChatArchitectureSignal[];
};

export const PMA_CHAT_ARCHITECTURE_TARGET_SCORE = 0.92;

export const PMA_CHAT_ARCHITECTURE_GOAL: Record<PmaChatArchitecturePrinciple, string> = {
  backendTranscriptProjection:
    'Render the backend transcript projection as the source of truth; the frontend may hold only pending optimistic user rows.',
  thinRouteSurface:
    'Keep Svelte routes focused on binding UI controls to application services, not owning transcript or stream reconciliation rules.',
  applicationCommandBoundary:
    'Plan and execute chat commands through typed application-layer functions so command behavior is easy to unit test.',
  capabilityAdapters:
    'Plug new chat capabilities in through explicit command, stream, and projection adapters instead of page-local branching.',
  deterministicRepair:
    'Prefer cursor streams with snapshot repair and deterministic reconciliation over recurring quiet refresh loops.',
  windowedRendering:
    'Keep chat indexes and transcripts bounded or virtualized so large workspaces do not degrade page behavior.',
  contractTests:
    'Cover projection, command, stream, and optimistic reconciliation behavior with deterministic tests.'
};

export type PmaChatArchitectureGoalInput = {
  transcriptProjectionIsBackendOwned: boolean;
  routeOwnsTranscriptReconciliation: boolean;
  commandsUseApplicationPlans: boolean;
  capabilitiesHaveTypedBoundaries: boolean;
  usesCursorStreamRepair: boolean;
  usesUnboundedRendering: boolean;
  hasContractTests: boolean;
};

export function evaluatePmaChatArchitectureGoal(
  input: PmaChatArchitectureGoalInput
): PmaChatArchitectureGoalEvaluation {
  const signals: PmaChatArchitectureSignal[] = [
    signal(
      'backendTranscriptProjection',
      input.transcriptProjectionIsBackendOwned,
      4,
      PMA_CHAT_ARCHITECTURE_GOAL.backendTranscriptProjection
    ),
    signal(
      'thinRouteSurface',
      !input.routeOwnsTranscriptReconciliation,
      3,
      PMA_CHAT_ARCHITECTURE_GOAL.thinRouteSurface
    ),
    signal(
      'applicationCommandBoundary',
      input.commandsUseApplicationPlans,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.applicationCommandBoundary
    ),
    signal(
      'capabilityAdapters',
      input.capabilitiesHaveTypedBoundaries,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.capabilityAdapters
    ),
    signal(
      'deterministicRepair',
      input.usesCursorStreamRepair,
      3,
      PMA_CHAT_ARCHITECTURE_GOAL.deterministicRepair
    ),
    signal(
      'windowedRendering',
      !input.usesUnboundedRendering,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.windowedRendering
    ),
    signal(
      'contractTests',
      input.hasContractTests,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.contractTests
    )
  ];
  const totalWeight = signals.reduce((total, item) => total + item.weight, 0);
  const satisfiedWeight = signals
    .filter((item) => item.satisfied)
    .reduce((total, item) => total + item.weight, 0);
  const score = totalWeight === 0 ? 0 : satisfiedWeight / totalWeight;
  return {
    score,
    targetScore: PMA_CHAT_ARCHITECTURE_TARGET_SCORE,
    satisfied: score >= PMA_CHAT_ARCHITECTURE_TARGET_SCORE,
    strengths: signals.filter((item) => item.satisfied).map((item) => item.detail),
    gaps: signals.filter((item) => !item.satisfied).map((item) => item.detail),
    signals
  };
}

export function mergeTranscriptSnapshotWithPendingOptimistic(
  existing: { cardsById: Record<string, ChatTranscriptCard>; order: string[] } | null | undefined,
  backendRows: ChatTranscriptCard[]
): ChatTranscriptCard[] {
  if (!existing) return backendRows;
  const retainedOptimistic = existing.order
    .filter((id) => id.startsWith('optimistic:'))
    .map((id) => existing.cardsById[id])
    .filter((card): card is ChatTranscriptCard => Boolean(card))
    .filter((card) => !transcriptRowsConfirmOptimistic(backendRows, card));
  return [...backendRows, ...retainedOptimistic];
}

export function transcriptRowsConfirmOptimistic(
  backendRows: ChatTranscriptCard[],
  optimistic: ChatTranscriptCard
): boolean {
  if (optimistic.kind !== 'message' || optimistic.message.role !== 'user') return true;
  const optimisticCorrelationId = transcriptCardCorrelationId(optimistic);
  if (!optimisticCorrelationId) return false;
  return backendRows.some((row) => {
    if (row.id.startsWith('optimistic:')) return false;
    if (row.kind !== 'message' || row.message.role !== 'user') return false;
    return transcriptCardCorrelationId(row) === optimisticCorrelationId;
  });
}

export function transcriptCardCorrelationId(card: ChatTranscriptCard): string | null {
  if (card.kind !== 'message') return null;
  const raw = card.message.raw;
  const direct = raw.correlation_id ?? raw.client_turn_id;
  if (typeof direct === 'string' && direct.trim()) return direct.trim();
  const identity = raw.identity;
  if (identity && typeof identity === 'object' && !Array.isArray(identity)) {
    const value = (identity as Record<string, unknown>).correlation_id;
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function signal(
  principle: PmaChatArchitecturePrinciple,
  satisfied: boolean,
  weight: number,
  detail: string
): PmaChatArchitectureSignal {
  return { principle, satisfied, weight, detail };
}
