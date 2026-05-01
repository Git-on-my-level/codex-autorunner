# CAR Telegram Slash Commands Guide (Codex app-server v2)

This guide specifies how to implement “built-in” Codex CLI-style slash commands in Codex Autorunner (CAR) when CAR talks to Codex via the **codex app-server v2 JSON-RPC** protocol.

Key point: **Slash commands are client-side control commands.** Do **not** forward `/review` or `/model` as normal `turn/start` text. Instead, parse and route them to the correct JSON-RPC methods (or CAR-local actions).

---

## Goal

Codex CLI “slash commands” (e.g. `/review`, `/model`) are **client-side commands** in the CLI UI. The **app-server does not parse `/…` inside `turn/start`**. If your Telegram integration forwards `/review` as plain text to `turn/start`, Codex will treat it as a normal prompt and your bot will respond “unsupported command” (because your bot’s command router didn’t handle it), or the model will try to interpret it.

To add support, the Telegram path must:

1. **Detect** a slash command in the incoming Telegram message.
2. **Handle it in CAR** by calling the appropriate **app-server JSON-RPC method(s)** (or by applying overrides that will be included in the *next* `turn/start`).
3. **Render** the result back to Telegram.

This guide documents the built-in command set and how to implement each one using **only app-server behavior** (method names, enum values, request/response payload shapes). You can hand this directly to an implementation agent.

---

## 0) App-server basics you must assume

### 0.1 Transport and envelope

App-server speaks a JSON-RPC-like protocol over a stream (typically stdio). Requests are JSON objects:

* Requests have: `{"id": <int>, "method": "<string>", "params": {...}}`
* Notifications have: `{"method": "<string>", "params": {...}}`
* Responses: `{"id": <int>, "result": {...}}` or `{"id": <int>, "error": {...}}`

**Important:** the app-server messages **do not include** `"jsonrpc": "2.0"`.

### 0.2 Initialize handshake (if your client isn’t already doing it)

You typically do:

```json
{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "car-telegram", "title": "CAR Telegram", "version": "0.1.0"}}}
{"method": "initialized"}
```

After that, you can call v2 APIs like `thread/start`, `turn/start`, `review/start`, etc.

### 0.3 Thread mapping (Telegram chat ⇄ Codex thread)

To support commands, you need a per-chat state:

* `threadId` (string)
* current effective settings (model, reasoning effort, approval policy, sandbox policy, cwd)
* any “pending overrides” to apply on next `turn/start` (if you implement commands that update settings without starting a turn)

---

## 1) Canonical list of built-in slash commands

These are the CLI’s built-in slash command strings (kebab-case; you should support these exact names):

* `/model` — choose model + reasoning effort
* `/approvals` — choose what can run without approval (approval + sandbox)
* `/experimental` — toggle beta features
* `/skills` — list skills available
* `/review` — run code review on current changes
* `/new` — start a new chat/thread
* `/resume` — resume a saved thread
* `/init` — generate `AGENTS.md`
* `/compact` — compact/summarize conversation (CLI-only; see notes)
* `/diff` — show git diff (including untracked)
* `/mention` — mention a file (CLI input helper; see notes)
* `/status` — show session config and token usage
* `/mcp` — list configured MCP tools
* `/logout` — log out
* `/feedback` — send logs/feedback
* `/rollout` — print rollout path (debug/unstable in CLI; but thread metadata contains a path)
* `/ps` — list background terminals (CLI-only; see notes)
* `/test-approval` — debug-only (ignore for production)

### 1.1 “Available during task” semantics (optional but matches CLI behavior)

In the CLI UI, these are **not** available while a turn is running:

* `/new`, `/resume`, `/init`, `/compact`, `/model`, `/approvals`, `/experimental`, `/review`, `/logout`

The rest are allowed during a task.

For Telegram, the simplest compatible behavior is:

* If there is an active turn for the chat’s thread, reject these commands with a “busy” message (or implement queuing).

---

## 2) Telegram command parsing rules (recommended)

### 2.1 Prefer Telegram entities over naïve prefix checks

Telegram messages can contain `entities` where one may be of type `bot_command`. Prefer that over `text.startswith("/")`, because `/mnt/data/...` can be misread as a command.

### 2.2 Normalize command token

When the command token is like `/review@YourBotName`, normalize to `review`.

Also normalize to lower-case and accept kebab-case as-is.

### 2.3 Suggested grammar for args

Because CLI uses interactive popups, Telegram must encode selections in args. Use a simple format:

* `/model` or `/model list`
* `/model set <model> [effort] [--persist]`
* `/review [uncommitted|pr [branch]|base <branch>|commit <sha>|custom <instructions>] [--detached]`
* `/approvals [preset <read-only|auto|full-access>|set <approvalPolicy> <sandboxPreset>] [--persist]`
* `/experimental [list|set <featureKey> <on|off>]`
* `/resume [list|<threadId>]`

You can implement a smaller subset, but the payload mappings below assume this shape.

---

## 3) Method and enum reference (concrete values)

You will need these **exact values** when constructing params.

### 3.1 Approval policy enum (`approvalPolicy` / `approval_policy`)

Allowed values (kebab-case):

* `"untrusted"`
* `"on-failure"`
* `"on-request"`
* `"never"`

### 3.2 Reasoning effort enum (`effort` / `model_reasoning_effort`)

Allowed values (lowercase):

* `"none"`
* `"minimal"`
* `"low"`
* `"medium"`
* `"high"`
* `"xhigh"`

### 3.3 Thread sandbox mode enum (`thread/start` / `thread/resume` field: `sandbox`)

Allowed values (kebab-case):

* `"read-only"`
* `"workspace-write"`
* `"danger-full-access"`

### 3.4 Turn sandbox policy object (`turn/start` field: `sandboxPolicy`)

Tagged union: `{"type": ...}` with camelCase tags:

* Read-only:

  ```json
  {"type":"readOnly"}
  ```
* Workspace write (optional fields shown):

  ```json
  {
    "type":"workspaceWrite",
    "writableRoots": ["/absolute/path/optional"],
    "networkAccess": false,
    "excludeTmpdirEnvVar": false,
    "excludeSlashTmp": false
  }
  ```
* External sandbox:

  ```json
  {"type":"externalSandbox","networkAccess":"restricted"}   // or "enabled"
  ```
* Full access:

  ```json
  {"type":"dangerFullAccess"}
  ```

### 3.5 Review delivery enum (`review/start` field: `delivery`)

Allowed values:

* `"inline"`
* `"detached"`

### 3.6 Review target object (`review/start` field: `target`)

Tagged union: `{"type": ...}` with camelCase tags:

* Uncommitted changes:

  ```json
  {"type":"uncommittedChanges"}
  ```
* Base branch diff:

  ```json
  {"type":"baseBranch","branch":"main"}
  ```
* Commit:

  ```json
  {"type":"commit","sha":"<sha or ref>"}
  ```
* Custom:

  ```json
  {"type":"custom","instructions":"Focus on correctness and security."}
  ```

### 3.7 Config write merge strategy (`config/value/write` and `config/batchWrite`)

Allowed values (camelCase enum serialized lowercase):

* `"replace"`
* `"upsert"`

---

## 4) Command-by-command implementation guide

### 4.1 `/new` → start a new thread

**Intent:** create a new thread and associate it with the Telegram chat.

**API:** `thread/start`

**Request example:**

```json
{
  "id": 101,
  "method": "thread/start",
  "params": {
    "model": "gpt-5.1-codex",
    "cwd": "/repo",
    "approvalPolicy": "on-request",
    "sandbox": "workspace-write"
  }
}
```

**Response shape (key fields):**

```json
{
  "id": 101,
  "result": {
    "thread": {"id":"thr_abc", "preview":"", "path":"/.../rollout.jsonl", "cwd":"/repo", "...": "..."},
    "model":"gpt-5.1-codex",
    "modelProvider":"openai",
    "cwd":"/repo",
    "approvalPolicy":"on-request",
    "sandbox":{"type":"workspaceWrite","networkAccess":false,...},
    "reasoningEffort":"medium"
  }
}
```

**Implementation notes:**

* Save `threadId = result.thread.id` into chat state.
* Clear any per-thread “pending overrides”.
* You will also receive a `thread/started` notification; you can ignore it if you already stored the thread from the response.

---

### 4.2 `/resume` → list threads and resume one

#### A) `/resume list` → show recent threads

**API:** `thread/list`

**Request:**

```json
{"id": 201, "method":"thread/list", "params":{"cursor":null,"limit":10}}
```

**Response:**

```json
{
  "id": 201,
  "result": {
    "data": [
      {"id":"thr_1","preview":"Fix failing test","createdAt":1730000000,"cwd":"/repo","path":"/..."},
      {"id":"thr_2","preview":"Investigate bug","createdAt":1730000100,"cwd":"/repo","path":"/..."}
    ],
    "nextCursor": null
  }
}
```

Render a Telegram message like:

* `thr_1 — Fix failing test`
* `thr_2 — Investigate bug`

#### B) `/resume <threadId>` → attach to a thread

**API:** `thread/resume`

**Request:**

```json
{"id": 202, "method":"thread/resume", "params":{"threadId":"thr_1"}}
```

**Response:**

* Similar shape to `thread/start`, but includes the thread and effective config.
* In `thread.turns`, “items” are populated only in `thread/resume` responses (useful if you want to show context).

**Implementation notes:**

* Update chat state: set `threadId` to resumed thread id.
* Expect `thread/started` notification too (ignore or reconcile).

---

### 4.3 `/model` → list models and set model/effort

#### A) `/model` or `/model list` → list available models

**API:** `model/list`

**Request:**

```json
{"id": 301, "method":"model/list", "params":{"cursor":null,"limit":50}}
```

**Response fields:**

* `data[]` entries include:

  * `model` (string slug)
  * `displayName`, `description`
  * `supportedReasoningEfforts[]` (each has `reasoningEffort` + `description`)
  * `defaultReasoningEffort`
  * `isDefault`

**Example (truncated):**

```json
{
  "id": 301,
  "result": {
    "data": [
      {
        "id":"preset_1",
        "model":"gpt-5.1-codex",
        "displayName":"GPT‑5.1 Codex",
        "description":"Coding-focused model",
        "supportedReasoningEfforts":[
          {"reasoningEffort":"minimal","description":"Fastest"},
          {"reasoningEffort":"medium","description":"Balanced"},
          {"reasoningEffort":"high","description":"Most thorough"}
        ],
        "defaultReasoningEffort":"medium",
        "isDefault":true
      }
    ],
    "nextCursor":null
  }
}
```

Render a list (model slug + supported efforts).

#### B) `/model set <model> [effort]` → apply for subsequent turns

**Key app-server constraint:** there is **no dedicated “set model” method in v2**. You apply model and effort by sending them as overrides on the **next** `turn/start`.

So implement this as:

1. Store `pendingModel = <model>` and `pendingEffort = <effort|omit>`.
2. When the user sends a normal chat message, include them in that `turn/start`:

**Next `turn/start`:**

```json
{
  "id": 302,
  "method":"turn/start",
  "params":{
    "threadId":"thr_abc",
    "model":"gpt-5.1-codex",
    "effort":"high",
    "input":[{"type":"text","text":"Please refactor the parser."}]
  }
}
```

After that turn, clear `pendingModel/pendingEffort` (or keep them as “current overrides” in your session state if you want the user’s setting to persist in the Telegram chat UX).

#### C) Optional: `/model set ... --persist` → write to config.toml

If you want the selection to affect future sessions globally, call:

* `config/value/write` with `keyPath: "model"`
* and optionally `config/value/write` with `keyPath: "model_reasoning_effort"`

**Persist model:**

```json
{
  "id": 303,
  "method":"config/value/write",
  "params":{
    "keyPath":"model",
    "value":"gpt-5.1-codex",
    "mergeStrategy":"replace"
  }
}
```

**Persist effort:**

```json
{
  "id": 304,
  "method":"config/value/write",
  "params":{
    "keyPath":"model_reasoning_effort",
    "value":"high",
    "mergeStrategy":"replace"
  }
}
```

---

### 4.4 `/review` → run code review

**API:** `review/start`

**Default behavior suggestion for Telegram:** inline + uncommitted changes.

#### A) `/review` → uncommitted changes, inline

```json
{
  "id": 401,
  "method":"review/start",
  "params":{
    "threadId":"thr_abc",
    "target":{"type":"uncommittedChanges"},
    "delivery":"inline"
  }
}
```

**Response:**

```json
{
  "id": 401,
  "result": {
    "turn": {"id":"turn_1","status":"in_progress","items":[...maybe empty...]},
    "reviewThreadId":"thr_abc"
  }
}
```

Then you will receive normal turn/item streaming notifications (same as a `turn/start` turn). Your Telegram layer should reuse its existing streaming-to-message rendering.

#### B) `/review pr [branch]`

```json
{
  "id": 402,
  "method":"review/start",
  "params":{
    "threadId":"thr_abc",
    "target":{"type":"baseBranch","branch":"main"},
    "delivery":"inline"
  }
}
```

#### C) `/review base main`

```json
{
  "id": 403,
  "method":"review/start",
  "params":{
    "threadId":"thr_abc",
    "target":{"type":"baseBranch","branch":"main"},
    "delivery":"inline"
  }
}
```

#### D) `/review commit 9fceb02`

```json
{
  "id": 404,
  "method":"review/start",
  "params":{
    "threadId":"thr_abc",
    "target":{"type":"commit","sha":"9fceb02"},
    "delivery":"inline"
  }
}
```

#### E) `/review custom <instructions>`

```json
{
  "id": 405,
  "method":"review/start",
  "params":{
    "threadId":"thr_abc",
    "target":{"type":"custom","instructions":"Look for security issues and missing tests."},
    "delivery":"inline"
  }
}
```

#### F) Detached reviews (optional)

If you pass `delivery:"detached"`, the app-server will fork a new review thread, emit a `thread/started` for it, and return `reviewThreadId` as that new thread id. You can still surface the output to the user in Telegram without switching the chat’s “active thread”.

---

### 4.5 `/approvals` → set approval policy + sandbox policy

There is **no v2 request** like “set approvals now”. In practice you have two workable choices:

1. **Ephemeral, per Telegram chat:** store pending overrides and attach them to the next `turn/start`.
2. **Persistent:** write config via `config/value/write`.

#### A) Recommended preset behavior (matches CLI presets)

Support presets:

* `read-only`

  * approvalPolicy: `"on-request"`
  * sandboxPolicy: `{"type":"readOnly"}`
* `auto` (agent)

  * approvalPolicy: `"on-request"`
  * sandboxPolicy: `{"type":"workspaceWrite"}`
* `full-access`

  * approvalPolicy: `"never"`
  * sandboxPolicy: `{"type":"dangerFullAccess"}`

Example: `/approvals preset auto`

Store:

* `pendingApprovalPolicy = "on-request"`
* `pendingSandboxPolicy = {"type":"workspaceWrite"}`

Apply on next turn:

```json
{
  "id": 501,
  "method":"turn/start",
  "params":{
    "threadId":"thr_abc",
    "approvalPolicy":"on-request",
    "sandboxPolicy":{"type":"workspaceWrite","networkAccess":false},
    "input":[{"type":"text","text":"Continue."}]
  }
}
```

#### B) Optional persistence (`--persist`)

Persist approval policy:

```json
{
  "id": 502,
  "method":"config/value/write",
  "params":{
    "keyPath":"approval_policy",
    "value":"on-request",
    "mergeStrategy":"replace"
  }
}
```

Persist sandbox mode (thread sandbox is derived from this mode + sandbox config):

```json
{
  "id": 503,
  "method":"config/value/write",
  "params":{
    "keyPath":"sandbox_mode",
    "value":"workspace-write",
    "mergeStrategy":"replace"
  }
}
```

If you want network access in workspace mode, also write:

```json
{
  "id": 504,
  "method":"config/value/write",
  "params":{
    "keyPath":"sandbox_workspace_write.network_access",
    "value":true,
    "mergeStrategy":"replace"
  }
}
```

---

### 4.6 `/experimental` → toggle beta features

This is implemented as toggling feature flags in config. The beta features exposed by CLI are:

* `unified_exec` (background terminal)
* `shell_snapshot`

Use `config/value/write`:

Enable unified exec:

```json
{
  "id": 601,
  "method":"config/value/write",
  "params":{
    "keyPath":"features.unified_exec",
    "value":true,
    "mergeStrategy":"replace"
  }
}
```

Disable shell snapshot:

```json
{
  "id": 602,
  "method":"config/value/write",
  "params":{
    "keyPath":"features.shell_snapshot",
    "value":false,
    "mergeStrategy":"replace"
  }
}
```

To list current state, call `config/read` and look at `config.features`.

---

### 4.7 `/skills` → list skills

**API:** `skills/list`

Request (use current cwd by omitting `cwds`):

```json
{"id": 701, "method":"skills/list", "params": {"cwds": [], "forceReload": false}}
```

Response:

```json
{
  "id": 701,
  "result": {
    "data": [
      {
        "cwd":"/repo",
        "skills":[
          {"name":"pdfs","description":"Work with PDFs", "path":"/.../pdfs.md", "scope":"system"}
        ],
        "errors":[]
      }
    ]
  }
}
```

Render skill names and brief descriptions. Users can then invoke skills in normal messages (e.g., `$pdfs: ...`) if your system supports that convention.

---

### 4.8 `/mcp` → list MCP servers and tools

**API:** `mcpServerStatus/list`

```json
{"id": 801, "method":"mcpServerStatus/list", "params":{"cursor":null,"limit":50}}
```

Response includes `data[]` where each entry has:

* `name`
* `authStatus`
* `tools` (map)
* `resources`, `resourceTemplates`

Render a concise summary (server name + auth + tool names).

(If you later want OAuth login, the method is `mcpServer/oauth/login`, but it’s not a CLI slash command; optional.)

---

### 4.9 `/diff` → show git diff (including untracked files)

There is no dedicated “git diff” API. Use `command/exec`.

Minimal (tracked changes only):

```json
{
  "id": 901,
  "method":"command/exec",
  "params":{
    "command":["git","diff","--color"],
    "timeoutMs":60000,
    "cwd":"/repo"
  }
}
```

To mimic CLI behavior “including untracked”, you also need:

1. `git ls-files --others --exclude-standard`
2. for each untracked file, run `git diff --color --no-index -- /dev/null <file>`

That is multiple `command/exec` calls; in Telegram you may choose the minimal form.

---

### 4.10 `/status` → show thread config + token usage

There is no single `status` endpoint; assemble it from what you already have plus optional calls:

* From your chat state (saved from `thread/start` / `thread/resume`):

  * threadId, cwd, model, modelProvider, approvalPolicy, sandbox, reasoningEffort
* From streaming notifications you’ve already received:

  * `thread/tokenUsage/updated` gives totals + last-turn usage
* Optionally call:

  * `config/read` for global config
  * `account/read` or `account/rateLimits/read` if you want to show auth/rate-limit info

A practical output for Telegram:

* Thread: `thr_abc`
* CWD: `/repo`
* Model: `gpt-5.1-codex` (effort: `high`)
* Approval: `on-request`
* Sandbox: `workspaceWrite (networkAccess=false)`
* Token usage (last): input/output/total (if you track it)

---

### 4.11 `/init` → generate `AGENTS.md`

There is no dedicated API; the CLI implements this by sending a normal user prompt that instructs the agent to create `AGENTS.md`.

Use this **exact prompt** as the text input:

> Generate a file named AGENTS.md that serves as a contributor guide for this repository.
> Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.
> Follow the outline below, but adapt as needed — add sections if relevant, and omit those that do not apply to this project.
>
> Document Requirements
>
> * Title the document "Repository Guidelines".
> * Use Markdown headings (#, ##, etc.) for structure.
> * Keep the document concise. 200-400 words is optimal.
> * Keep explanations short, direct, and specific to this repository.
> * Provide examples where helpful (commands, directory paths, naming patterns).
> * Maintain a professional, instructional tone.
>
> Recommended Sections
> Project Structure & Module Organization
>
> * Outline the project structure, including where the source code, tests, and assets are located.
>
> Build, Test, and Development Commands
>
> * List key commands for building, testing, and running locally (e.g., npm test, make build).
> * Briefly explain what each command does.
>
> Coding Style & Naming Conventions
>
> * Specify indentation rules, language-specific style preferences, and naming patterns.
> * Include any formatting or linting tools used.
>
> Testing Guidelines
>
> * Identify testing frameworks and coverage requirements.
> * State test naming conventions and how to run tests.
>
> Commit & Pull Request Guidelines
>
> * Summarize commit message conventions found in the project’s Git history.
> * Outline pull request requirements (descriptions, linked issues, screenshots, etc.).
>
> (Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions.

Send it as a standard turn:

```json
{
  "id": 1001,
  "method":"turn/start",
  "params":{
    "threadId":"thr_abc",
    "input":[{"type":"text","text":"<PASTE PROMPT HERE>"}]
  }
}
```

---

### 4.12 `/logout` → log out

**API:** `account/logout`

Request:

```json
{"id": 1101, "method":"account/logout"}
```

Expect a response and also an `account/updated` notification (auth mode becomes null/none).

---

### 4.13 `/feedback` → upload feedback/logs

**API:** `feedback/upload`

Request example (include logs + associate with current thread):

```json
{
  "id": 1201,
  "method":"feedback/upload",
  "params":{
    "classification":"bug",
    "reason":"Telegram output formatting breaks on long messages.",
    "threadId":"thr_abc",
    "includeLogs":true
  }
}
```

Response includes a `threadId` (the thread associated with the feedback record).

---

### 4.14 `/rollout` → show rollout path

The thread metadata includes a `path` field (thread file path on disk). This is marked unstable, but it exists in thread objects returned by `thread/start`, `thread/resume`, and `thread/list`.

Implementation:

* If you have the current thread object stored, print `thread.path`.
* If not, call `thread/list` and find the matching `threadId`.

---

## 5) Commands that are CLI-only or UI helpers (what to do in Telegram)

### 5.1 `/compact`

CLI triggers internal context compaction. **There is no v2 request** to compact.

Options for Telegram:

* Mark as unsupported: “`/compact` is not available via app-server.”
* Approximation: start a new thread with a user-provided summary (costly and not equivalent).
* Advanced: resume thread history, ask model to summarize, then start new thread with summary (still not the same as internal compaction).

### 5.2 `/ps`

CLI lists “background terminals” (tied to unified exec). There is **no app-server request** to list them in v2. Treat as unsupported.

### 5.3 `/mention`

In CLI it inserts `@` to mention a file in the composer. In Telegram, users can type file paths directly. Treat as:

* no-op, or
* “Reply with the file path you want to reference.”

---

## 6) Recommended minimal set to implement first

If you want parity with “most-used” CLI commands for Telegram, implement in this order:

1. `/review` → `review/start`
2. `/model` → `model/list` + apply via next `turn/start`
3. `/approvals` → apply via next `turn/start` (presets)
4. `/new` → `thread/start`
5. `/resume` → `thread/list` + `thread/resume`
6. `/diff` → `command/exec git diff`
7. `/status` → render from stored session + token usage notifications
8. `/mcp` → `mcpServerStatus/list`
9. `/skills` → `skills/list`

---

## 7) Practical message-to-API routing examples

### Example: user sends `/review` in Telegram

1. CAR detects command `review` (no args).
2. CAR sends:

```json
{"id": 2001, "method":"review/start", "params":{"threadId":"thr_abc","target":{"type":"uncommittedChanges"},"delivery":"inline"}}
```

3. CAR continues reading event stream and renders the resulting assistant output back to Telegram as it already does for turns.

### Example: user sends `/model set gpt-5.1-codex high`

1. CAR stores:

* pending model = `gpt-5.1-codex`
* pending effort = `high`

2. CAR replies in Telegram: “Model set to gpt-5.1-codex (high). Will apply on next message.”
3. Next user normal message triggers `turn/start` with `model` + `effort`.

---

This is sufficient to implement CLI-like slash commands over the app-server protocol without access to Codex CLI source.
