# Spec

## Context
- The V2 web UI exposes editable docs (TODO/PROGRESS/OPINIONS/SPEC) but editing is manual, so users must hand-author changes even for obvious rewrites and formatting fixes.
- We already have a chat-capable Codex backend and streaming/log plumbing; this feature should let users “chat with a doc” to ask for targeted updates without leaving the Docs tab.
- “Chat with docs” means: from a doc view, type a natural language request; the system gathers the doc text plus broader repo/doc context, runs an agent to rewrite the doc, and applies the edits automatically.

## Goals
- Add a per-doc chat box in the Docs tab that sends a request tied to the currently selected doc.
- The backend runs a doc-focused agent using the chosen doc + wider context to produce an updated doc file, saves it, and returns the new text.
- The UI shows request/response status, streams or displays the agent’s message, and updates the doc editor/preview without a manual refresh.
- All activity is logged and does not interfere with the main runner loop or terminal sessions.

## User stories
- As a user on the Docs tab, I can ask “rewrite TODO item 3 to be more specific” on the TODO doc and see the TODO markdown updated automatically, with the new text loaded into the editor.
- As a user, I can ask “summarize last week’s changes in PROGRESS” and get a response plus the PROGRESS doc rewritten accordingly.
- As a user, I see errors (e.g., Codex failure, validation failure) inline in the chat box and my doc is left unchanged if the agent fails.

## Requirements
- **UI/UX**
  - Each doc view shows a “Chat with this doc” panel at the bottom containing: multiline input, Send button, running indicator, latest agent response, and a small history list scoped to that doc for the current page session.
  - The Send button is disabled while a request is in flight; Enter+Shift inserts newline, Enter sends.
  - Successful responses replace the editor content with the returned doc text and refresh any derived views (TODO preview, outstanding counts).
  - Error states display a concise message and keep prior content intact; user can resend.
  - Optional streaming: if the backend streams tokens, the panel renders partial text; otherwise it shows the full response on completion.
- **API**
  - Add `POST /api/docs/{kind}/chat` where `kind` ∈ {todo, progress, opinions, spec}.
  - Request payload: `{ "message": str, "stream": bool? }`.
  - Response on non-stream path: `{ "status": "ok", "kind": "...", "content": "<updated doc>", "agent_message": "<summary/notes>" }`; on error: `{ "status": "error", "detail": "..." }` with 4xx/5xx.
  - Streaming (if enabled) uses SSE: events for `status` (queued/running), `token` (agent text), `update` (final doc content), `error`, `done`.
  - Request is rejected with 400 for unknown doc kinds or empty messages; 409 if another doc chat is already running for the same doc (prevent overlapping edits).
- **Agent behavior**
  - Builds prompt with: target doc full text, other work docs (TODO/PROGRESS/OPINIONS/SPEC), recent run summary if available (last run block clipped), and user message.
  - Clear instructions: edit only the target doc, keep markdown structure/checkbox syntax intact, and respond with the fully rewritten doc (not a diff).
  - Runs via Codex CLI in a bounded mode (e.g., single `exec` command) with time and token limits; captures stdout/stderr into the log with a distinct “doc-chat” marker.
  - Validates output: must be non-empty string; may optionally parse TODO checkboxes to ensure format; rejects obviously malformed output.
  - On success, writes the updated doc atomically to disk and returns it; on validation failure, surfaces an error without writing.
- **State and concurrency**
  - Doc chat runs independently of the main runner loop but respects the same repo lock to avoid simultaneous file writes; if the loop is running, queue or fail fast with a clear message.
  - If the user has unsaved edits in the editor, warn before overwriting (e.g., detect textarea divergence and prompt to confirm).
  - Log entries include run id/time, doc kind, user message, success/error, and a pointer to the updated file.
- **Testing/telemetry**
  - Add backend unit tests covering: API validation, prompt assembly, happy-path file write, validation failures, and lock conflicts.
  - Add a lightweight UI test (if feasible) for the chat panel send/response flow using mocked fetch/stream.

## Non-goals
- Multi-doc edits in one request; the agent should only modify the selected doc.
- Long-lived conversational memory across page reloads or sessions (history is page-local only).
- Changing the main runner prompt or workflow; doc chat is an on-demand helper, not part of the continuous loop.

## Open questions
- Should streaming be required or optional? Default to non-streaming if complexity is high; streaming can be incremental.
- Should we gate doc chat when the runner is mid-loop to avoid conflicts, or allow it with best-effort merge? (Default: block with a clear message.)
