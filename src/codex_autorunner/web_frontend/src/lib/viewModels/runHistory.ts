import type { JsonRecord } from '$lib/api/client';
import type { WorkStatus } from '$lib/viewModels/domain';
import { worktreeTicketRoute } from '$lib/viewModels/routes';

export type RunHistoryEntry = {
  id: string;
  title: string;
  status: WorkStatus;
  summary: string | null;
  timestamp: string | null;
  href: string | null;
  attempts: number;
};

export function runHistoryFromAutomationJobs(jobs: JsonRecord[]): RunHistoryEntry[] {
  return jobs.map((job, index) => {
    const id = stringValue(job.jobId ?? job.job_id, `job-${index + 1}`);
    const state = stringValue(job.state, 'idle');
    const worktreeId = nullableString(job.ticketFlowWorktreeId ?? job.ticket_flow_worktree_id);
    const managedThreadId = nullableString(job.managedThreadTargetId ?? job.managed_thread_target_id);
    const childExecution = recordValue(job.childExecution ?? job.child_execution);
    const queueResult = recordValue(job.pmaQueueResult ?? job.pma_queue_result);
    const queuePayloadResult = recordValue(queueResult?.result);
    const spawnedChatId = managedThreadId ?? nullableString(
      queuePayloadResult?.managed_thread_id ??
        queuePayloadResult?.managedThreadId ??
        queuePayloadResult?.thread_id ??
        queuePayloadResult?.threadId
    );
    return {
      id,
      title: `Run ${shortId(id)}`,
      status: statusFromJobState(state),
      summary: nullableString(job.resultSummary ?? job.result_summary) ?? nullableString(job.errorText ?? job.error_text),
      timestamp:
        nullableString(job.finishedAt ?? job.finished_at) ??
        nullableString(job.updatedAt ?? job.updated_at) ??
        nullableString(job.createdAt ?? job.created_at),
      href:
        nullableString(childExecution?.chat_href ?? childExecution?.target_href) ??
        (spawnedChatId ? `/chats/${encodeURIComponent(spawnedChatId)}` : worktreeId ? worktreeTicketRoute(worktreeId) : null),
      attempts: numberValue(job.attemptCount ?? job.attempt_count, 0)
    };
  });
}

export function statusFromJobState(state: string): WorkStatus {
  const normalized = state.trim().toLowerCase();
  if (normalized === 'succeeded' || normalized === 'done') return 'done';
  if (normalized === 'failed') return 'failed';
  if (normalized === 'running') return 'running';
  if (normalized === 'pending' || normalized === 'waiting') return 'waiting';
  return 'idle';
}

function shortId(id: string): string {
  const trimmed = id.trim();
  return (trimmed || 'job').slice(0, 8);
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberValue(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function recordValue(value: unknown): JsonRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : null;
}
