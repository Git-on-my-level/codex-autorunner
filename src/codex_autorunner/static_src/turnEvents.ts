import { fetchActiveFileChat, streamTurnEvents, type ActiveTurnPayload, type TurnEventMeta } from "./fileChat.js";

export interface ResumeOptions {
  basePath?: string;
  eventsBasePath?: string;
  onEvent?(payload: unknown): void;
  onResult?(result: Record<string, unknown>): void;
  onError?(msg: string): void;
}

export interface ResumeOutcome {
  controller: AbortController | null;
  lastResult?: Record<string, unknown>;
}

export async function resumeFileChatTurn(
  clientTurnId: string,
  opts: ResumeOptions = {}
): Promise<ResumeOutcome> {
  const active: ActiveTurnPayload = await fetchActiveFileChat(clientTurnId, opts.basePath || "/api/file-chat/active");
  const current = (active.current || {}) as Record<string, unknown>;
  const lastResult = (active.last_result || {}) as Record<string, unknown>;

  if (lastResult.status && opts.onResult) {
    opts.onResult(lastResult);
  }

  const threadId = typeof current.thread_id === "string" ? current.thread_id : "";
  const turnId = typeof current.turn_id === "string" ? current.turn_id : "";
  const agent = typeof current.agent === "string" ? current.agent : "codex";

  if (threadId && turnId) {
    const meta: TurnEventMeta = {
      agent,
      threadId,
      turnId,
      basePath: opts.eventsBasePath || "/api/file-chat/turns",
    };
    const controller = streamTurnEvents(meta, {
      onEvent: opts.onEvent,
      onError: opts.onError,
    });
    return { controller, lastResult };
  }

  return { controller: null, lastResult };
}
