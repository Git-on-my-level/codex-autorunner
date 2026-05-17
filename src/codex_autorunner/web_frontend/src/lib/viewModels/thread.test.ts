import { describe, expect, it } from 'vitest';
import { mapThreadSummary, mapThreadDetail } from './thread';

describe('mapThreadSummary', () => {
  it('maps managed thread payload with repo scope', () => {
    const vm = mapThreadSummary({
      thread_target_id: 'thread-1',
      display_name: 'Repo fix',
      agent_id: 'codex',
      lifecycle_status: 'active',
      normalized_status: 'idle',
      repo_id: 'repo-1',
      latest_execution: { model: 'gpt-5.2', started_at: '2026-05-04T00:00:00Z' }
    });

    expect(vm.id).toBe('thread-1');
    expect(vm.title).toBe('Repo fix');
    expect(vm.status).toBe('idle');
    expect(vm.agentId).toBe('codex');
    expect(vm.model).toBe('gpt-5.2');
    expect(vm.scope).toEqual({ kind: 'repo', id: 'repo-1' });
    expect(vm.ticketId).toBeNull();
  });

  it('maps worktree-scoped thread', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-wt',
      agent: 'codex',
      status: 'running',
      resource_kind: 'worktree',
      resource_id: 'wt-1',
      base_repo_id: 'base-repo',
      updated_at: '2026-05-04T00:00:00Z'
    });

    expect(vm.id).toBe('thread-wt');
    expect(vm.scope).toEqual({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base-repo' });
  });

  it('maps hub-scoped thread', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-hub',
      name: 'New PMA chat',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.id).toBe('thread-hub');
    expect(vm.scope).toEqual({ kind: 'hub' });
  });

  it('maps unrecognized resource_kind thread payloads to hub scope', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-ws',
      resource_kind: 'custom_workspace',
      resource_id: 'ws-1',
      agent: 'opencode',
      status: 'running'
    });

    expect(vm.scope).toEqual({ kind: 'hub' });
  });

  it('extracts ticket id from ticket flow thread', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-tf',
      name: 'ticket-flow:hermes@m4-pma',
      agent: 'codex',
      status: 'running',
      repo_id: 'codex-autorunner',
      resource_kind: 'worktree',
      resource_id: 'discord-5',
      last_message_preview:
        '<CAR_TICKET_FLOW_PROMPT><CAR_CURRENT_TICKET_FILE>PATH: .codex-autorunner/tickets/TICKET-330-test.md</CAR_CURRENT_TICKET_FILE></CAR_TICKET_FLOW_PROMPT>',
      updated_at: '2026-05-04T00:00:00Z'
    });

    expect(vm.ticketId).toBe('TICKET-330-test');
  });

  it('extracts surface from surface_urn', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-surf',
      surface_urn: 'managed_thread:thread-surf',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.surface).toEqual({ kind: 'managed_thread', key: 'thread-surf' });
  });

  it('extracts surface from surface_kind/surface_key', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-surf2',
      surface_kind: 'discord',
      surface_key: 'channel-123',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.surface).toEqual({ kind: 'discord', key: 'channel-123' });
  });

  it('returns null surface when not present', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-nosurf',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.surface).toBeNull();
  });

  it('builds readable title from first message excerpt', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-excerpt',
      name: 'New PMA chat',
      first_message_excerpt: 'Please fix the login bug',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.title).toBe('Please fix the login bug');
  });

  it('strips injected context from display title', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-inj',
      display_name:
        '<injected context>\nCAR managed repo\n</injected context>\n\nFix login',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.title).toBe('Fix login');
  });

  it('builds title from scope for generic chat name', () => {
    const vm = mapThreadSummary({
      managed_thread_id: 'thread-generic',
      name: 'New PMA chat',
      repo_id: 'my-repo',
      agent: 'codex',
      status: 'idle'
    });

    expect(vm.title).toBe('Chat · my-repo');
  });
});

describe('mapThreadDetail', () => {
  it('extends summary with detail fields', () => {
    const vm = mapThreadDetail({
      managed_thread_id: 'thread-detail',
      agent: 'codex',
      status: 'running',
      repo_id: 'repo-1',
      body: 'Detailed thread content',
      turn_count: 5,
      last_turn_id: 'turn-5'
    });

    expect(vm.id).toBe('thread-detail');
    expect(vm.body).toBe('Detailed thread content');
    expect(vm.turnCount).toBe(5);
    expect(vm.lastTurnId).toBe('turn-5');
    expect(vm.scope).toEqual({ kind: 'repo', id: 'repo-1' });
  });
});
