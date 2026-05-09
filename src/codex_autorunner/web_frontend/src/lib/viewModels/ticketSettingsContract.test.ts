import { describe, expect, it } from 'vitest';
import { agentIdsFromPmaAgentsPayload, PMA_REASONING_EFFORT_VALUES } from './ticketSettingsContract';

describe('ticketSettingsContract', () => {
  it('dedupes, lowercases, and sorts agent ids from PMA /hub/pma/agents payloads', () => {
    expect(
      agentIdsFromPmaAgentsPayload([
        { id: 'Codex', name: 'Codex' },
        { id: 'codex', name: 'Codex dup' },
        { id: 'OpenCode', name: 'OpenCode' }
      ])
    ).toEqual(['codex', 'opencode']);
  });

  it('matches adapters/chat/model_selection.REASONING_EFFORT_VALUES', () => {
    expect([...PMA_REASONING_EFFORT_VALUES]).toEqual(['none', 'minimal', 'low', 'medium', 'high', 'xhigh']);
  });
});
