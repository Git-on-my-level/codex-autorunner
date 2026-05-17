# Unified Automation Plane

CAR automation is represented by normalized events, durable rules, durable
jobs, job attempts, and schedules in hub orchestration state. Built-in behavior
uses the same records as user-created behavior.

## Invariants

- Lifecycle and SCM inputs are recorded as `AutomationEvent` rows before rule
  evaluation.
- Enabled `AutomationRule` rows are the only source of job creation for covered
  PMA and SCM behavior.
- `AutomationJob` dedupe keys are durable, so repeated event processing after a
  restart reuses the existing job.
- Disabled rules may still coexist with recorded events, but they do not enqueue
  jobs.
- `ticket_flow` automation jobs always resolve a base repo/worktree into an
  isolated automation worktree before materializing tickets.
- PMA subscriptions and timers remain API-compatible adapters. They mirror into
  automation rules and schedules, and any retained wakeup rows are backfilled
  into automation events/jobs before execution.
- GitHub reaction config seeds built-in SCM automation rules. Publish side
  effects execute through automation jobs and publish-operation attempts, not a
  permanent reaction fast path.

## Target Policies

`existing_repo`, `existing_worktree`, `new_automation_worktree`,
`auto_worktree`, and `pr_worktree` select the base for work. For `ticket_flow`,
that target is never mutated directly: it is used to create or reuse a
deterministic automation worktree such as `automation/<rule>/<job>` or
`automation/pr-<number>/<rule>`.

`hub` is reserved for PMA coordination and internal publish jobs that do not
mutate a repo checkout.

## Failure Handling

Jobs retry according to their policy and record each attempt. When retry budget
is exhausted, the job becomes dead-lettered. Explicit `on_failure` policy may
enqueue another job, typically a PMA turn, for operator escalation.
