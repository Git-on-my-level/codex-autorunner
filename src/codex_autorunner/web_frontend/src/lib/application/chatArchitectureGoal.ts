export type ChatArchitecturePrinciple =
  | 'backendTranscriptProjection'
  | 'thinRouteSurface'
  | 'applicationCommandBoundary'
  | 'capabilityAdapters'
  | 'deterministicRepair'
  | 'windowedRendering'
  | 'contractTests';

export type ChatArchitectureSignal = {
  principle: ChatArchitecturePrinciple;
  satisfied: boolean;
  weight: number;
  detail: string;
};

export type ChatArchitectureGoalEvaluation = {
  score: number;
  targetScore: number;
  satisfied: boolean;
  strengths: string[];
  gaps: string[];
  signals: ChatArchitectureSignal[];
};

export const CHAT_ARCHITECTURE_TARGET_SCORE = 0.92;

export const CHAT_ARCHITECTURE_GOAL: Record<ChatArchitecturePrinciple, string> = {
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

export type ChatArchitectureGoalInput = {
  transcriptProjectionIsBackendOwned: boolean;
  routeOwnsTranscriptReconciliation: boolean;
  commandsUseApplicationPlans: boolean;
  capabilitiesHaveTypedBoundaries: boolean;
  usesCursorStreamRepair: boolean;
  usesUnboundedRendering: boolean;
  hasContractTests: boolean;
};

export function evaluateChatArchitectureGoal(
  input: ChatArchitectureGoalInput
): ChatArchitectureGoalEvaluation {
  const signals: ChatArchitectureSignal[] = [
    signal(
      'backendTranscriptProjection',
      input.transcriptProjectionIsBackendOwned,
      4,
      CHAT_ARCHITECTURE_GOAL.backendTranscriptProjection
    ),
    signal(
      'thinRouteSurface',
      !input.routeOwnsTranscriptReconciliation,
      3,
      CHAT_ARCHITECTURE_GOAL.thinRouteSurface
    ),
    signal(
      'applicationCommandBoundary',
      input.commandsUseApplicationPlans,
      2,
      CHAT_ARCHITECTURE_GOAL.applicationCommandBoundary
    ),
    signal(
      'capabilityAdapters',
      input.capabilitiesHaveTypedBoundaries,
      2,
      CHAT_ARCHITECTURE_GOAL.capabilityAdapters
    ),
    signal(
      'deterministicRepair',
      input.usesCursorStreamRepair,
      3,
      CHAT_ARCHITECTURE_GOAL.deterministicRepair
    ),
    signal(
      'windowedRendering',
      !input.usesUnboundedRendering,
      2,
      CHAT_ARCHITECTURE_GOAL.windowedRendering
    ),
    signal(
      'contractTests',
      input.hasContractTests,
      2,
      CHAT_ARCHITECTURE_GOAL.contractTests
    )
  ];
  const totalWeight = signals.reduce((total, item) => total + item.weight, 0);
  const satisfiedWeight = signals
    .filter((item) => item.satisfied)
    .reduce((total, item) => total + item.weight, 0);
  const score = totalWeight === 0 ? 0 : satisfiedWeight / totalWeight;
  return {
    score,
    targetScore: CHAT_ARCHITECTURE_TARGET_SCORE,
    satisfied: score >= CHAT_ARCHITECTURE_TARGET_SCORE,
    strengths: signals.filter((item) => item.satisfied).map((item) => item.detail),
    gaps: signals.filter((item) => !item.satisfied).map((item) => item.detail),
    signals
  };
}

function signal(
  principle: ChatArchitecturePrinciple,
  satisfied: boolean,
  weight: number,
  detail: string
): ChatArchitectureSignal {
  return { principle, satisfied, weight, detail };
}
