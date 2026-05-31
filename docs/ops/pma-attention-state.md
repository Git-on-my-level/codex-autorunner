# PMA Attention State

PMA prompt turns use an attention-state overview for the every-turn
`<current_actionable_state>` block. The full hub snapshot remains available on
first turns and through explicit drilldowns, but repeat turns should not expose
snapshot plumbing, queue precedence internals, or stale idle inventory as if it
were new work.

The attention state is derived from the existing action queue and hub snapshot.
It does not introduce a second source of truth for precedence, supersession, or
protected-thread semantics.

## Idle Overview

```text
<pma v=2 mode=overview state=idle fresh=idle actions=0>
q:
  none
bg:
  threads protected=8 reusable=1 cleanup=0 running=0 failed=0 hung=0
  files fresh=0 stale=3
  repos tracked=25 dirty=2
  automation pending=0
drill:
  car hub snapshot --section managed_threads
  car pma files
  car hub snapshot --section repos
  car pma automation list
</pma>
```

Idle background surfaces are counts, not prompts to browse. PMA should drill
down only when the user asks about a surface or when an action item points at
that surface.

## Action Overview

```text
<pma v=2 mode=overview state=action fresh=ok actions=2>
q:
  1 src=dispatch scope=run:run-123 repo=discord-1 run=run-123 fresh=ok cmd="car hub snapshot --section action_queue"
  2 src=thread scope=thread:thread-456 repo=discord-3 thread=thread-456 fresh=ok cmd="car pma thread info --id thread-456"
bg:
  threads protected=8 reusable=1 cleanup=0 running=1 failed=1 hung=0
  files fresh=0 stale=3
  repos tracked=25 dirty=2
  automation pending=0
drill:
  car hub snapshot --section action_queue
  car hub snapshot --section managed_threads
  car pma files
  car hub snapshot --section repos
  car pma automation list
semantic_ref=4f80cce5d8d2ef8a
</pma>
```

The command is the recommendation. Prose fields such as `why_selected`,
`recommended_detail`, raw `basis_at`, digest previews, and state keys belong in
drilldown/debug views, not the default PMA prompt packet.

If more than three hard-action rows exist, PMA receives the total action count,
a `+N more` queue row, and `car hub snapshot --section action_queue` as the explicit
drilldown.

## Unchanged Delta

```text
<context_unchanged />
```

When durable docs are cached and the attention overview has not changed, repeat
turns collapse to a one-bit signal. A changed hub snapshot alone does not force
the attention packet back into the prompt if the derived attention state is
unchanged. The attention digest includes hidden action semantics such as
`next_action`, `recommended_action`, `recommended_detail`, and `open_url`, so
semantic action changes still re-emit `<current_actionable_state>`.
