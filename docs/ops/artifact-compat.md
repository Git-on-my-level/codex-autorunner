# Artifact compatibility operations

Normal agent-facing delivery should use the artifact journal:

```bash
car artifacts send <file> --to current
```

This page is for operators debugging broken target injection, stranded files, or
old FileBox handoffs. Do not copy these compatibility paths into PMA prompts,
generated repo docs, or normal chat-agent instructions.

## Current target diagnostics

`--to current` requires these runtime environment variables:

- `CAR_ARTIFACT_TARGET_SURFACE`
- `CAR_ARTIFACT_TARGET_CONVERSATION_KEY`
- `CAR_ARTIFACT_WORKSPACE_SCOPE` when the target is workspace-scoped

If the first two variables are missing, fix the chat/runtime target injection
before asking an agent to send files.

## Explicit operator send

Use an explicit target only when debugging current-target wiring:

```bash
car artifacts send <file> --to explicit --surface <surface> --conversation <conversation>
```

Add `--workspace-scope <scope>` when the target is scoped to a repo or hub.

## Legacy FileBox import

Legacy files may still exist under:

- `.codex-autorunner/filebox/outbox/`
- `.codex-autorunner/filebox/outbox/pending/`

Import already-created legacy files into the journal with:

```bash
car artifacts import-legacy
```

Discord and Telegram compatibility drains may still import and deliver these
files for old workflows. The journal remains the delivery source of truth for
new workflows.
