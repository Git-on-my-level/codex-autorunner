import { describe, expect, it } from 'vitest';
import {
  agentCanListModels,
  agentDisplayForChat,
  agentId,
  agentLabel,
  agentProfileEntriesForRecord,
  agentRecordForId,
  firstModelValue,
  modelExists,
  modelLabel,
  modelRecordForValue,
  pickerReasoningOptions,
  resolveAgentModelSelection,
  resolveChatSelectorsForActiveChat
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

  it('shows catalog agent label on chat rows when known, otherwise raw id', () => {
    expect(agentDisplayForChat(agents, { agentId: 'codex' })).toBe('Codex');
    expect(agentDisplayForChat(agents, { agentId: 'unknown-bot' })).toBe('unknown-bot');
    expect(agentDisplayForChat(agents, { agentId: null })).toBe('');
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

  it('normalizes Hermes profile rows from PMA /agents payloads', () => {
    const hermes = {
      id: 'hermes',
      name: 'Hermes',
      profiles: [
        { id: 'planning', display_name: 'Planning mode' },
        { id: 'global', display_name: 'global' }
      ],
      capability_projection: {
        actions: { list_models: { allowed: false, missing_capabilities: ['model_listing'] } }
      }
    };
    expect(agentProfileEntriesForRecord(hermes)).toEqual([
      { id: 'global', label: 'global' },
      { id: 'planning', label: 'Planning mode' }
    ]);
    expect(agentProfileEntriesForRecord(agents[0])).toEqual([]);
    expect(agentLabel(hermes)).toBe('Hermes');
  });

  it('resolves model and reasoning selection from the shared catalog rules', () => {
    expect(
      resolveAgentModelSelection({
        agents,
        agentId: 'hermes',
        catalog: models,
        preferredModel: 'openai/gpt-5.4',
        currentReasoning: 'high',
        keepReasoning: true
      })
    ).toEqual({ canListModels: false, model: '', reasoning: '' });

    expect(
      resolveAgentModelSelection({
        agents,
        agentId: 'codex',
        catalog: models,
        preferredModel: 'missing',
        rememberedModel: 'anthropic/claude'
      })
    ).toEqual({ canListModels: true, model: 'anthropic/claude', reasoning: '' });

    expect(
      resolveAgentModelSelection({
        agents,
        agentId: 'codex',
        catalog: models,
        currentReasoning: 'high',
        keepReasoning: true
      })
    ).toEqual({ canListModels: true, model: 'openai/gpt-5.4', reasoning: 'high' });

    expect(
      resolveAgentModelSelection({
        agents,
        agentId: 'codex',
        catalog: models,
        currentReasoning: 'high',
        keepReasoning: true,
        allowEmptyModel: true
      })
    ).toEqual({ canListModels: true, model: '', reasoning: '' });
  });

  it('keeps existing chats with unknown projected models empty even when picker memory exists', () => {
    const incidentAgents = [
      ...agents,
      {
        id: 'zai-coding-plan',
        name: 'Z.ai Coding Plan',
        capability_projection: { actions: { list_models: { allowed: true, missing_capabilities: [] } } }
      }
    ];
    const incidentCatalog = [
      { id: 'zai-coding-plan/glm-5.1', label: 'GLM 5.1' },
      { id: 'zai-coding-plan/glm-5v-turbo', label: 'GLM 5V Turbo' }
    ];
    const rememberedModel = 'zai-coding-plan/glm-5v-turbo';
    const resolved = resolveChatSelectorsForActiveChat(
      {
        id: 'chat-zai',
        title: 'Existing Z.ai chat',
        lifecycleStatus: 'active',
        status: 'running',
        agentId: 'zai-coding-plan',
        chatKind: 'coding_agent',
        agentProfile: null,
        model: null,
        runtimeSource: 'unknown',
        modelSource: 'unknown',
        repoId: null,
        worktreeId: null,
        ticketId: null,
        isTicketFlow: false,
        progressPercent: null,
        updatedAt: null,
        raw: {}
      },
      incidentAgents,
      'zai-coding-plan',
      ''
    );

    expect(resolved).toMatchObject({ mode: 'chat-bound', agentId: 'zai-coding-plan', model: null });
    expect(
      resolveAgentModelSelection({
        agents: incidentAgents,
        agentId: resolved.agentId,
        catalog: incidentCatalog,
        rememberedModel,
        allowEmptyModel: true
      }).model
    ).toBe('zai-coding-plan/glm-5v-turbo');
    expect(
      resolveAgentModelSelection({
        agents: incidentAgents,
        agentId: resolved.agentId,
        catalog: incidentCatalog,
        preferredModel: resolved.mode === 'chat-bound' ? resolved.model : null,
        rememberedModel: null,
        currentModel: null,
        allowEmptyModel: true
      })
    ).toEqual({ canListModels: true, model: '', reasoning: '' });
  });
});
