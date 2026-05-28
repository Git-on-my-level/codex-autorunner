import { describe, expect, it } from 'vitest';
import { evaluateChatArchitectureGoal } from './chatArchitectureGoal';

describe('chat architecture goal', () => {
  it('treats backend projection plus application boundaries as the target architecture', () => {
    const result = evaluateChatArchitectureGoal({
      transcriptProjectionIsBackendOwned: true,
      routeOwnsTranscriptReconciliation: false,
      commandsUseApplicationPlans: true,
      capabilitiesHaveTypedBoundaries: true,
      usesCursorStreamRepair: true,
      usesUnboundedRendering: false,
      hasContractTests: true
    });

    expect(result.satisfied).toBe(true);
    expect(result.score).toBe(1);
    expect(result.gaps).toEqual([]);
  });

  it('penalizes page-owned orchestration and unbounded rendering even when commands are typed', () => {
    const result = evaluateChatArchitectureGoal({
      transcriptProjectionIsBackendOwned: true,
      routeOwnsTranscriptReconciliation: true,
      commandsUseApplicationPlans: true,
      capabilitiesHaveTypedBoundaries: false,
      usesCursorStreamRepair: true,
      usesUnboundedRendering: true,
      hasContractTests: true
    });

    expect(result.satisfied).toBe(false);
    expect(result.gaps).toContain(
      'Keep Svelte routes focused on binding UI controls to application services, not owning transcript or stream reconciliation rules.'
    );
    expect(result.gaps).toContain(
      'Keep chat indexes and transcripts bounded or virtualized so large workspaces do not degrade page behavior.'
    );
  });
});
