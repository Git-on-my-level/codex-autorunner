import { describe, expect, it } from 'vitest';
import { runHistoryFromAutomationJobs, statusFromJobState } from './runHistory';

describe('statusFromJobState', () => {
  it('maps automation job states to work status pills', () => {
    expect(statusFromJobState('succeeded')).toBe('done');
    expect(statusFromJobState('done')).toBe('done');
    expect(statusFromJobState('failed')).toBe('failed');
    expect(statusFromJobState('running')).toBe('running');
    expect(statusFromJobState('pending')).toBe('waiting');
    expect(statusFromJobState('waiting')).toBe('waiting');
    expect(statusFromJobState('claimed')).toBe('idle');
  });
});

describe('runHistoryFromAutomationJobs', () => {
  it('builds compact run rows from automation jobs', () => {
    const rows = runHistoryFromAutomationJobs([
      {
        job_id: 'job-1234567890',
        state: 'failed',
        created_at: '2026-05-17T09:00:00Z',
        updated_at: '2026-05-17T09:03:00Z',
        finished_at: '2026-05-17T09:05:00Z',
        error_text: 'Scan failed',
        attempt_count: 2,
        ticket_flow_worktree_id: 'wt-1'
      }
    ]);

    expect(rows).toEqual([
      {
        id: 'job-1234567890',
        title: 'Run job-1234',
        status: 'failed',
        summary: 'Scan failed',
        timestamp: '2026-05-17T09:05:00Z',
        href: '/worktrees/wt-1/tickets',
        attempts: 2
      }
    ]);
  });

  it('prefers result summaries and falls back through timestamps', () => {
    const [row] = runHistoryFromAutomationJobs([
      {
        jobId: 'job-1',
        state: 'succeeded',
        createdAt: '2026-05-17T09:00:00Z',
        updatedAt: '2026-05-17T09:03:00Z',
        resultSummary: 'Clean scan'
      }
    ]);

    expect(row.summary).toBe('Clean scan');
    expect(row.timestamp).toBe('2026-05-17T09:03:00Z');
    expect(row.href).toBeNull();
  });

  it('links managed-thread automation runs to spawned chats', () => {
    const [row] = runHistoryFromAutomationJobs([
      {
        job_id: 'job-pma',
        state: 'running',
        child_execution: {
          chat_href: '/chats/thread-target-1',
          target_href: '/worktrees/wt-ignored/tickets'
        },
        ticket_flow_worktree_id: 'wt-ignored'
      }
    ]);

    expect(row.href).toBe('/chats/thread-target-1');
  });

  it('uses effective state and durable child graph links from workspace scope', () => {
    const [row] = runHistoryFromAutomationJobs([
      {
        job_id: 'job-stale-parent',
        state: 'running',
        effective_state: 'succeeded',
        children: [
          {
            child_kind: 'agent_task',
            child_id: 'exec-turn-wrong-id',
            terminal_state: 'succeeded',
            requested_runtime: {
              workspace_scope: {
                target_kind: 'thread',
                target_id: 'thread-target-2'
              }
            }
          }
        ]
      }
    ]);

    expect(row.status).toBe('done');
    expect(row.href).toBe('/chats/thread-target-2');
  });
});
