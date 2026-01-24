# Codex app-server developer guide

Codex `app-server` is a **stdio-based JSON-RPC–style** API surface for embedding Codex inside another program (GUI, editor extension, agent runner, etc.). Your app acts as the **client**: it launches `codex app-server` as a subprocess, writes newline-delimited JSON requests to stdin, and reads newline-delimited JSON responses/notifications from stdout.

This guide documents the **current protocol surface** (recommended: **thread/turn v2 APIs**) and the expected message flow to build a client on top of it.

---

## 1) How to run it

Run as a subprocess:

```bash
codex app-server
```

The server communicates **only over stdin/stdout** (no HTTP port).

Useful tooling:

* Generate TypeScript bindings for the protocol:

  ```bash
  codex app-server generate-ts --out DIR
  ```
* Generate a JSON Schema bundle:

  ```bash
  codex app-server generate-json-schema --out DIR
  ```

Use those generated artifacts as the **source of truth** for exact request/response shapes and enum values for the specific Codex version you are embedding.

---

## 2) Transport and framing

### Newline-delimited JSON

* Every message is a **single JSON object on a single line**.
* Do **not** pretty-print requests across multiple lines.
* The server ignores blank lines.

### JSON-RPC–like envelopes (no `"jsonrpc": "2.0"` field required)

#### Client → Server request

```json
{"id":"<string-or-number>","method":"<methodName>","params":{...}}
```

#### Server → Client response

```json
{"id":"<same id>","result":{...}}
```

#### Server → Client error (for a request)

```json
{"id":"<same id>","error":{"code":-32600,"message":"...","data":{...}}}
```

#### Server → Client notification (unsolicited)

```json
{"method":"<eventName>","params":{...}}
```

#### Server → Client request (server-initiated; you must respond)

Same shape as a normal request, but originated by the server. Used primarily for **approvals**:

```json
{"id":0,"method":"item/commandExecution/requestApproval","params":{...}}
```

### Request IDs

* `id` can be a string or number.
* Recommended: **client uses UUID strings** to avoid collisions with server-generated numeric IDs (server uses integers starting at 0 for its own requests).

---

## 3) Initialization handshake

### 3.1 `initialize`

**Method:** `initialize`

**Params:**

```ts
{
  clientInfo: {
    name: string
    title?: string
    version: string
  }
}
```

**Result:**

```ts
{ userAgent: string }
```

### 3.2 `initialized` (client notification)

After a successful `initialize` response, send:

```json
{"method":"initialized"}
```

---

## 4) Core concepts

### Thread

A **thread** is the persisted session container (roughly “conversation”). Threads are identified by a string ID (in practice, a UUID string).

### Turn

A **turn** is one “run” inside a thread (one user request / task). A turn produces a stream of:

* lifecycle notifications (`turn/started`, `turn/completed`)
* item notifications (`item/started`, `item/completed`)
* incremental deltas for streaming text/output (e.g. `item/agentMessage/delta`)

### Item

A **thread item** is a typed unit within a turn, such as:

* `userMessage`
* `agentMessage`
* `reasoning`
* `commandExecution`
* `fileChange`
* `mcpToolCall`
* etc.

Items are surfaced in:

* `item/started` / `item/completed` notifications (full object snapshots)
* and sometimes as deltas (e.g. agent message text streaming)

---

## 5) Recommended lifecycle: thread + turn (v2 API)

### 5.1 Create a new thread

**Method:** `thread/start`

**Params (high level):**

```ts
{
  model?: string
  modelProvider?: string
  cwd?: string
  approvalPolicy?: AskForApproval
  sandbox?: SandboxMode
  config?: Record<string, any>
  baseInstructions?: string
  developerInstructions?: string
  experimentalRawEvents?: boolean
}
```

**Result:**

```ts
{
  thread: Thread
  model: string
  modelProvider: string
  cwd: string
  approvalPolicy: AskForApproval
  sandbox: SandboxPolicy
  reasoningEffort?: ReasoningEffort
}
```

**Behavior notes:**

* The server **auto-attaches an event listener** for the thread and will emit structured notifications during turns.
* You typically receive a `thread/started` notification shortly after the response.

### 5.2 Run a turn in that thread

**Method:** `turn/start`

**Params:**

```ts
{
  threadId: string
  input: UserInput[]           // usually [{type:"text", text:"..."}]
  cwd?: string                 // overrides persist for subsequent turns
  approvalPolicy?: AskForApproval
  sandboxPolicy?: SandboxPolicy
  model?: string
  effort?: ReasoningEffort
  summary?: ReasoningSummary
}
```

**Result:**

```ts
{ turn: Turn }
```

**Behavior notes:**

* The `turn/start` response includes a `turn` object with `status: InProgress`.
* Streaming output arrives via notifications (section 7).

### 5.3 Resume an existing thread

**Method:** `thread/resume`

**Params (common path):**

```ts
{
  threadId: string
  // optional overrides:
  model?: string
  modelProvider?: string
  cwd?: string
  approvalPolicy?: AskForApproval
  sandbox?: SandboxMode
  config?: Record<string, any>
  baseInstructions?: string
  developerInstructions?: string
}
```

**Result:** same shape as `thread/start`, but the returned `thread` typically includes turn history (see below).

**History loading:**

* `thread/resume` is the primary way to get a **full reconstructed history** (turns and items). Other APIs typically return “thin” thread/turn objects and rely on notifications for live updates.

### 5.4 List and archive threads

* **`thread/list`**: cursor/limit pagination
* **`thread/archive`**: archives a thread ID (empty result)

---

## 6) Method reference

### Threads

#### `thread/start`

Create a new thread and persist it.

#### `thread/resume`

Load an existing thread by `threadId`. Also supports (unstable) resume by rollout path or provided history.

#### `thread/list`

**Params:** `{ cursor?: string, limit?: number, modelProviders?: string[] }`
**Result:** `{ data: Thread[], nextCursor?: string }`

#### `thread/archive`

**Params:** `{ threadId: string }`
**Result:** `{}`

---

### Turns

#### `turn/start`

Start work in a thread with `input: UserInput[]`.

`UserInput`:

* `{ type: "text", text: string }`
* `{ type: "image", url: string }`
* `{ type: "localImage", path: string }`

#### `turn/interrupt`

**Params:** `{ threadId: string, turnId: string }`
**Result:** `{}`

---

### Reviews

#### `review/start`

Start a code review turn.

**Params:**

```ts
{
  threadId: string
  target: ReviewTarget
  delivery?: "inline" | "detached"
}
```

`ReviewTarget`:

* `{ type: "uncommittedChanges" }`
* `{ type: "baseBranch", branch: string }`
* `{ type: "commit", sha: string, title?: string }`
* `{ type: "custom", instructions: string }`

**Result:**

```ts
{
  turn: Turn
  reviewThreadId: string   // if detached, this is a new thread ID
}
```

---

### Models

#### `model/list`

Paginated list of models and their reasoning options.

**Params:** `{ cursor?: string, limit?: number }`
**Result:** `{ data: Model[], nextCursor?: string }`

---

### Skills

#### `skills/list`

Returns discovered “skills” for one or more working directories.

**Params:**

```ts
{
  cwds?: string[]       // empty => current session cwd
  forceReload?: boolean
}
```

**Result:** `{ data: SkillsListEntry[] }`

---

### Sandbox command execution (utility)

#### `command/exec`

Executes an argv vector under the server’s sandboxing rules.

**Params:**

```ts
{
  command: string[]        // argv
  timeoutMs?: number
  cwd?: string
  sandboxPolicy?: SandboxPolicy
}
```

**Result:**

```ts
{ exitCode: number, stdout: string, stderr: string }
```

---

### Configuration

#### `config/read`

Reads effective config plus origin metadata.

**Params:** `{ includeLayers?: boolean }`
**Result:** `{ config: Config, origins: Record<string, ConfigLayerMetadata>, layers?: ConfigLayer[] }`

#### `config/value/write`

Writes one config key path.

**Params:**

```ts
{
  keyPath: string
  value: any
  mergeStrategy: "replace" | "upsert"
  filePath?: string
  expectedVersion?: string
}
```

**Result:** `ConfigWriteResponse` (includes status, new version, written file path)

#### `config/batchWrite`

Batch edit multiple key paths.

**Params:**

```ts
{
  edits: Array<{ keyPath: string, value: any, mergeStrategy: "replace" | "upsert" }>
  filePath?: string
  expectedVersion?: string
}
```

**Result:** `ConfigWriteResponse`

**Config write errors:**

* Reported as JSON-RPC error `code = -32600` with `error.data.config_write_error_code` set (e.g. version conflict, validation error, readonly layer).

---

### Account and auth

#### `account/read`

**Params:** `{ refreshToken?: boolean }`
**Result:** `{ account?: Account, requiresOpenaiAuth: boolean }`

`Account` is a tagged union:

* `{ type: "apiKey" }`
* `{ type: "chatgpt", email: string, planType: ... }`

#### `account/login/start`

**Params:**

* `{ type: "apiKey", apiKey: string }`
* `{ type: "chatgpt" }`

**Result:**

* `{ type: "apiKey" }`
* `{ type: "chatgpt", loginId: string, authUrl: string }`

Completion is delivered via the notification `account/login/completed`.

#### `account/login/cancel`

**Params:** `{ loginId: string }`
**Result:** `{ status: "canceled" | "notFound" }`

#### `account/logout`

**Params:** omitted
**Result:** `{}`

#### `account/rateLimits/read`

**Params:** omitted
**Result:** `{ rateLimits: RateLimitSnapshot }`

---

### MCP server integration

#### `mcpServerStatus/list`

Returns configured MCP servers, tools, resources, and auth status.

**Params:** `{ cursor?: string, limit?: number }`
**Result:** `{ data: McpServerStatus[], nextCursor?: string }`

#### `mcpServer/oauth/login`

Starts OAuth flow for a named MCP server.

**Params:** `{ name: string, scopes?: string[], timeoutSecs?: number }`
**Result:** `{ authorizationUrl: string }`

Completion arrives via `mcpServer/oauthLogin/completed`.

---

### Feedback

#### `feedback/upload`

**Params:**

```ts
{
  classification: string
  reason?: string
  threadId?: string
  includeLogs: boolean
}
```

**Result:** `{ threadId: string }`

---

### Legacy utility

#### `fuzzyFileSearch`

Fuzzy file search across roots.

**Params:** `{ query: string, roots: string[], cancellationToken?: string }`
**Result:** `{ files: Array<{ root, path, fileName, score, indices? }> }`

---

## 7) Notifications you must handle (server → client)

Your client should treat notifications as an **event stream** and be resilient to:

* unknown events (ignore/log)
* out-of-order arrivals
* repeated updates

### Lifecycle

* `thread/started` → `{ thread }`
* `turn/started` → `{ threadId, turn }`
* `turn/completed` → `{ threadId, turn }`

### Turn-level updates

* `turn/diff/updated` → `{ threadId, turnId, diff }` (aggregated unified diff for the turn)
* `turn/plan/updated` → `{ threadId, turnId, explanation?, plan: TurnPlanStep[] }`
* `thread/tokenUsage/updated` → `{ threadId, turnId, tokenUsage }`
* `thread/compacted` → `{ threadId, turnId }`

### Item snapshots

* `item/started` → `{ threadId, turnId, item }`
* `item/completed` → `{ threadId, turnId, item }`

### Streaming deltas

* `item/agentMessage/delta` → `{ threadId, turnId, itemId, delta }`
* `item/commandExecution/outputDelta` → `{ threadId, turnId, itemId, delta }`
* `item/fileChange/outputDelta` → `{ threadId, turnId, itemId, delta }`
* `item/mcpToolCall/progress` → `{ threadId, turnId, itemId, message }`
* `item/commandExecution/terminalInteraction` → `{ threadId, turnId, itemId, processId, stdin }`
* Reasoning deltas:

  * `item/reasoning/summaryTextDelta`
  * `item/reasoning/summaryPartAdded`
  * `item/reasoning/textDelta`

### Auth + account notifications

* `account/login/completed` → `{ loginId?: string, success: boolean, error?: string }`
* `account/updated` → `{ authMode?: "apiKey" | "chatgpt" }`
* `account/rateLimits/updated` → `{ rateLimits }`
* `mcpServer/oauthLogin/completed` → `{ name, success, error? }`

### Warnings and deprecations

* `deprecationNotice` → `{ summary, details? }`
* `windows/worldWritableWarning` → `{ samplePaths: string[], extraCount: number, failedScan: boolean }`

### Legacy event stream (`codex/event/*`)

Even when using v2 APIs, the server can emit legacy notifications:

* `method`: `codex/event/<EventMsg>`
* `params`: a serialized event object plus `conversationId`

Treat these as **unstable** unless you intentionally build against them.

---

## 8) Server-initiated approval requests (you must respond)

During a turn, Codex may request approval before executing commands or applying file changes.

### 8.1 Command execution approval

**Server request method:** `item/commandExecution/requestApproval`

**Params:**

```ts
{
  threadId: string
  turnId: string
  itemId: string
  reason?: string
  proposedExecpolicyAmendment?: ExecPolicyAmendment
}
```

**Your response result:**

```ts
{ decision: ApprovalDecision }
```

`ApprovalDecision` supports:

* `"accept"`
* `"acceptForSession"`
* `"decline"`
* `"cancel"`
* `{"acceptWithExecpolicyAmendment": { "execpolicyAmendment": string[] }}`

### 8.2 File change approval

**Server request method:** `item/fileChange/requestApproval`

**Params:**

```ts
{
  threadId: string
  turnId: string
  itemId: string
  reason?: string
  grantRoot?: string
}
```

**Your response result:** same `{ decision: ApprovalDecision }`

### Response envelope example

If the server sent:

```json
{"id":0,"method":"item/fileChange/requestApproval","params":{...}}
```

Respond:

```json
{"id":0,"result":{"decision":"accept"}}
```

---

## 9) Important enums and policy objects

### AskForApproval

Controls when Codex asks for approvals:

* `"untrusted"`
* `"on-failure"`
* `"on-request"`
* `"never"`

### SandboxMode (high-level)

Passed to `thread/start` / `thread/resume`:

* `"read-only"`
* `"workspace-write"`
* `"danger-full-access"`

### SandboxPolicy (detailed)

Used in turn overrides and `command/exec`. Tagged union:

* `{ "type": "readOnly" }`
* `{ "type": "dangerFullAccess" }`
* `{ "type": "workspaceWrite", "writableRoots": string[], "networkAccess": boolean, ... }`
* `{ "type": "externalSandbox", "networkAccess": "restricted" | "enabled" }`

---

## 10) Error handling expectations

### JSON-RPC error codes (common)

* `-32600` Invalid request (unknown method, invalid params, etc.)
* `-32603` Internal error

### Config write failures

Config write failures are surfaced as:

* JSON-RPC error with `code = -32600`
* `error.data.config_write_error_code` describing the specific reason (readonly layer, version conflict, validation error, etc.)

### Turn-level failures

Turn execution failures are surfaced via:

* `turn/completed` with `turn.status = Failed` and `turn.error`
* and/or `error` notifications (some may indicate transient failures with `willRetry = true`)

---

## 11) Minimal client architecture (recommended)

1. Spawn `codex app-server`.
2. Start a **read loop** on stdout:

   * parse each line as JSON
   * route by envelope type:

     * response (`id` + `result`)
     * error (`id` + `error`)
     * notification (`method` without `id`)
     * server request (`id` + `method` + `params`) → handle approvals and respond
3. Implement a request manager:

   * map `id → Promise/Deferred`
4. Treat notifications as authoritative for live UI:

   * update turn state on `turn/started` / `turn/completed`
   * render incremental output from delta events
   * render final item objects from `item/completed`

---

## 12) Deprecated (v1) API surface

Methods like `newConversation`, `sendUserMessage`, `addConversationListener`, `resumeConversation`, etc. are still present but are considered legacy. Prefer:

* `thread/*`
* `turn/*`
* structured v2 notifications

If you must interoperate with older clients, generate bindings (`generate-ts`) and implement both sets explicitly.
