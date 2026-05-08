import { describe, expect, it } from 'vitest';
import {
  agentCanListModels,
  agentId,
  agentRecordForId,
  firstModelValue,
  modelExists,
  modelLabel,
  modelRecordForValue,
  pickerReasoningOptions
} from './modelPickers';

describe('model picker helpers', () => {
  const agents = [
    {
      id: 'codex',
      name: 'Codex',
      capability_projection: { actions: { list_models: { allowed: true, missing_capabilities: [] } } }
    },
    {
      id: 'hermes',
      name: 'Hermes',
      capability_projection: { actions: { list_models: { allowed: false, missing_capabilities: ['model_listing'] } } }
    }
  ];

  const models = [
    { id: 'openai/gpt-5.4', label: 'GPT-5.4', reasoning_options: ['medium', 'high'] },
    { model: 'anthropic/claude', name: 'Claude', reasoning_options: [] }
  ];

  it('uses capability projection as the source of truth for model listing support', () => {
    expect(agentId(agents[0])).toBe('codex');
    expect(agentCanListModels(agentRecordForId(agents, 'codex'))).toBe(true);
    expect(agentCanListModels(agentRecordForId(agents, 'hermes'))).toBe(false);
    expect(agentCanListModels(null)).toBe(false);
  });

  it('normalizes model identity and reasoning options from catalog records', () => {
    expect(firstModelValue(models)).toBe('openai/gpt-5.4');
    expect(modelExists(models, 'anthropic/claude')).toBe(true);
    expect(modelLabel(models[1])).toBe('Claude');
    expect(modelRecordForValue(models, 'openai/gpt-5.4')).toBe(models[0]);
    expect(pickerReasoningOptions(models, 'openai/gpt-5.4')).toEqual(['medium', 'high']);
    expect(pickerReasoningOptions(models, 'anthropic/claude')).toEqual([]);
    expect(
      pickerReasoningOptions(
        [{ id: 'zai-coding-plan/glm-4.7', supports_reasoning: false, reasoning_options: ['none', 'minimal', 'high'] }],
        'zai-coding-plan/glm-4.7'
      )
    ).toEqual([]);
  });
});
