# Telegram Integration Specification

This document describes the normative specifications for the Telegram integration, including dispatch behavior, outbox operations, trigger mode, and security controls.

## Outbox Specification

The Telegram outbox provides reliable message delivery with retry logic, coalescing, and per-chat scheduling.

### Outbox Records

An `OutboxRecord` represents a pending message to be delivered:

- `record_id`: Unique identifier for the outbox record
- `chat_id`: Target Telegram chat ID
- `thread_id`: Optional thread/forum topic ID
- `reply_to_message_id`: Optional message ID to reply to
- `placeholder_message_id`: Optional placeholder message ID to delete after delivery
- `text`: Message content to send
- `created_at`: ISO timestamp of record creation
- `attempts`: Number of delivery attempts made (default 0)
- `last_error`: Last error message (truncated to 500 chars)
- `last_attempt_at`: ISO timestamp of last attempt
- `next_attempt_at`: ISO timestamp for next retry (optional)
- `operation`: Type of operation (e.g., "send", default "send")
- `message_id`: ID of sent message (populated after success)
- `outbox_key`: Optional key for coalescing records

### Outbox Keys and Coalescing

Records are coalesced by `outbox_key`, which is computed as:

```
outbox_key = f"{chat_id}:{thread_id or 'root'}:{message_id or 'new'}:{operation or 'send'}"
```

When multiple records share the same `outbox_key`, only the most recent is delivered. Older duplicates are discarded.

### Retry Behavior

The outbox uses a two-phase retry strategy:

1. **Immediate retries**: For transient failures, retry immediately with exponential backoff:
   - Attempt 1: 0.5s delay
   - Attempt 2: 2.0s delay
   - Attempt 3: 5.0s delay
   - Then proceed to scheduled retry

2. **Scheduled retries**: After immediate retries are exhausted, or when Telegram returns a `retry-after` header:
   - Wait for `next_attempt_at` timestamp
   - Retry every `OUTBOX_RETRY_INTERVAL_SECONDS` (default 10s) if ready

3. **Give up**: After `OUTBOX_MAX_ATTEMPTS` (default 8) failed attempts, the record is abandoned:
   - All records with the same `outbox_key` are deleted
   - If a `placeholder_message_id` exists, it's edited with: "Delivery failed after retries. Please resend."

### Outbox Manager Lifecycle

The `TelegramOutboxManager` runs two concurrent operations:

1. **Main loop**: Runs every `OUTBOX_RETRY_INTERVAL_SECONDS` (10s)
   - Lists all pending outbox records
   - Filters records ready for delivery (`next_attempt_at` is None or past)
   - Coalesces by `outbox_key`
   - Processes ready records

2. **Immediate delivery**: For messages requiring immediate send with retries
   - Enqueues the record
   - Waits and retries synchronously until success or max attempts

### Inflight Tracking

The outbox tracks messages inflight using `outbox_key` (or `record_id` if no `outbox_key`). Only one record with a given key can be processed at a time. Duplicate attempts return `False` from `_mark_inflight`.

## Trigger Mode Specification

The Telegram integration supports two trigger modes for starting agent runs:

### Modes

- `all` (default): Any message in an allowed chat/topic triggers a run
- `mentions`: Only messages that explicitly invoke the bot trigger a run

### Mentions Mode Triggering Conditions

A message triggers a run in `mentions` mode if ANY of the following are true:

1. **Private chat**: `message.chat_type == "private"` (always triggers)

2. **Explicit mention**: The bot username appears in the message text:
   - Pattern: `@{bot_username}` (case-insensitive)
   - Can appear anywhere in the text

3. **Reply to bot message**: The message is a reply to a message from this bot:
   - `message.reply_to_is_bot == true`
   - Excludes the "implicit topic reply" case where `reply_to_message_id == thread_id`

4. **Reply to bot by username**: The message is a reply to a user with the bot's username:
   - `message.reply_to_username.lower() == bot_username.lower()`
   - Excludes the implicit topic reply case

### Implicit Topic Reply Exception

Forum topics in Telegram have a UX where clients set `reply_to_message_id == thread_id` for messages that aren't actually replies to a specific message. This is called the "implicit topic reply" case and is excluded from reply-based triggering to avoid spam.

The condition is:

```
implicit_topic_reply = (
    thread_id is not None
    and reply_to_message_id is not None
    and reply_to_message_id == thread_id
)
```

When `implicit_topic_reply` is true, reply-to checks are skipped.

## Dispatch Specification

The dispatch system routes Telegram updates to appropriate handlers.

### Dispatch Context

A `DispatchContext` is built for each update:

- `chat_id`: Chat ID from message or callback
- `user_id`: User ID from message or callback
- `thread_id`: Thread/forum topic ID
- `message_id`: Message ID (for messages) or callback message ID
- `is_topic`: Whether the message is in a forum topic
- `is_edited`: Whether the message was edited
- `topic_key`: Computed routing key for this conversation

### Update Routing Flow

1. **Build context**: Extract metadata from update and resolve `topic_key`
2. **Set conversation ID**: Call `set_conversation_id(topic_key)` for log correlation
3. **Log received**: Log `telegram.update.received` with correlation fields
4. **Deduplicate**: Check if `update_id` was already processed for this `topic_key`
   - If duplicate, log `telegram.update.duplicate` and return
5. **Allowlist check**: Validate `chat_id` and `user_id` against allowlist
   - If denied, log `telegram.allowlist.denied` and return
6. **Route to handler**:
   - Callback: `update.callback` → `_dispatch_callback`
   - Message: `update.message` → `_dispatch_message`

### Callback Dispatch

Callbacks are routed to `_dispatch_callback`:

1. Parse callback data
2. Check if callback should bypass topic queue:
   - Approval, question, or interrupt callbacks bypass queue
3. If `topic_key` exists and not bypassing:
   - Enqueue work for topic (forces queue if in progress)
   - Return (work processes asynchronously)
4. Otherwise, handle immediately: `handlers._handle_callback(callback)`

### Message Dispatch

Messages are routed to `_dispatch_message`:

1. Check if message should bypass topic queue:
   - Commands and privileged messages may bypass
2. If `topic_key` exists and not bypassing:
   - Send queued placeholder if appropriate
   - Enqueue work for topic (forces queue)
   - Return (work processes asynchronously)
3. Otherwise, handle immediately: `handlers._handle_message(message)`

## Agent Invariants

The Telegram integration guarantees the following invariants:

### State Persistence

- All per-topic state is persisted to `telegram_state.sqlite3`
- State includes: workspace binding, active thread, thread summaries, approvals, outbox records
- State survives bot restarts and process crashes

### Topic Keys Are Unique

- A `topic_key` uniquely identifies a conversation
- Format: `"{chat_id}:{thread_id or 'root'}[:scope]"`
- Different scopes can exist for the same chat/thread combination

### At-Most-One Active Turn Per Topic

- Only one agent turn runs at a time for a given `topic_key`
- Incoming messages are queued via `_enqueue_topic_work` until the current turn completes
- Messages are processed in FIFO order per topic

### Outbox Exactly-Once Semantics

- A given `outbox_key` is delivered at most once
- After successful delivery, all records with that `outbox_key` are deleted
- Duplicate records (same `outbox_key`, different `record_id`) are coalesced

### Update Deduplication

- Each `update_id` is processed at most once per `topic_key`
- `last_update_id` is persisted per topic
- On restart, updates older than `last_update_id` are ignored

### Allowlist Enforcement

- All incoming updates (messages and callbacks) are validated against allowlists
- Both `allowed_chat_ids` and `allowed_user_ids` must be non-empty
- A message is denied if either check fails

### Placeholder Cleanup

- Placeholder messages are deleted after their associated outbox record succeeds
- If delivery fails, the placeholder is updated with a failure message
- Placeholders are never orphaned (tracked via `placeholder_message_id`)

## Security Allowlist and Shell Gating

### Allowlist Requirements

The Telegram enforces allowlist-based access control:

### Required Configuration

Both must be configured and non-empty:

- `allowed_chat_ids`: Set of Telegram chat IDs that can interact with the bot
- `allowed_user_ids`: Set of Telegram user IDs allowed to use the bot

If either set is empty, the bot refuses to handle all messages.

### Allowlist Check

For each incoming update (message or callback):

1. Extract `chat_id` and `user_id` from update
2. Verify `chat_id` is in `allowed_chat_ids`
3. Verify `user_id` is in `allowed_user_ids`
4. If `require_topics` is true, also verify `thread_id` is not None
5. If any check fails, log `telegram.allowlist.denied` and ignore the update

### Shell Command Gating

Shell commands (`!<cmd>`) are gated by:

1. **Enablement**: `telegram_bot.shell.enabled` must be `true` (default `false`)
2. **Allowlist**: User must pass allowlist check (same as normal messages)
3. **Workspace binding**: The topic must be bound to a workspace
4. **Approval/sandbox policy**: The Codex app-server enforces configured approval/sandbox settings

### Shell Command Flow

When a shell command is received:

1. Parse command: Strip leading `!` and trim whitespace
2. Validate shell is enabled (`telegram_bot.shell.enabled == true`)
3. Validate workspace is bound (workspace path exists)
4. Execute via Codex app-server with:
   - Configured `approval_policy`
   - Configured `sandbox_policy`
   - Output truncation (default 200 chars buffer)
   - Timeout (default 30 seconds)

### Approval Presets

The `/approvals` command sets approval mode and policy via presets:

- `read-only`: `approval_policy=on-request`, `sandbox_policy=readOnly`
- `auto`: `approval_policy=on-request`, `sandbox_policy=workspaceWrite`
- `full-access`: `approval_policy=never`, `sandbox_policy=dangerFullAccess`

Default approval mode is `yolo`, which maps to `full-access` preset.

### Security Considerations

1. **No auth beyond allowlists**: Telegram provides authentication via chat/user IDs, but there's no additional auth layer
2. **Multi-user setups**: Each operator should use their own bot token and instance to avoid cross-user interference
3. **Group chats**: Adding a group chat to `allowed_chat_ids` gives all members access (use with caution)
4. **Require topics**: Enforcing `require_topics=true` prevents accidental handling in the root of group chats
5. **Shell commands**: Treat `!<cmd>` as privileged; only enable when explicitly needed
6. **Data at rest**: State database contains workspace paths and thread IDs; protect accordingly
