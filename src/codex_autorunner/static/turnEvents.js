// GENERATED FILE - do not edit directly. Source: static_src/
import { fetchActiveFileChat, streamTurnEvents } from "./fileChat.js";
export async function resumeFileChatTurn(clientTurnId, opts = {}) {
    const active = await fetchActiveFileChat(clientTurnId, opts.basePath || "/api/file-chat/active");
    const current = (active.current || {});
    const lastResult = (active.last_result || {});
    if (lastResult.status && opts.onResult) {
        opts.onResult(lastResult);
    }
    const threadId = typeof current.thread_id === "string" ? current.thread_id : "";
    const turnId = typeof current.turn_id === "string" ? current.turn_id : "";
    const agent = typeof current.agent === "string" ? current.agent : "codex";
    if (threadId && turnId) {
        const meta = {
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
