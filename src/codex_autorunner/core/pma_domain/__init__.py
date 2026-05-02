"""PMA automation domain package — single authoritative source for policy.

This package owns all PMA automation policy types and domain events.
Adapters and runtime modules import canonical types from here rather than
redefining them locally.  Legacy modules that previously held parallel
implementations now delegate to this package.

Layer boundaries
----------------
- **Domain models** (this package): pure data types, normalization, and
  serialization.  No I/O, no filesystem access, no SQLite.
- **Domain policy** (``publish_policy``): owns duplicate/noop suppression
  decisions, notice classification, and publish message construction.
  Adapters and surfaces delegate to these functions instead of implementing
  ad hoc string checks or open-coded message rules.
- **Delivery lifecycle** (``delivery_lifecycle``): owns delivery-attempt
  state transitions, retry policy, and outcome classification.  The ledger
  records domain reasoning, not adapter error strings.
- **Rebinding policy** (``rebinding_policy``): owns the decision about
  what to do when a binding changes after a dispatch decision is persisted.
- **Subscription reducer** (``subscription_reducer``): owns subscription
  matching, WakeupIntent emission, and timer reduction.
- **Automation reducer** (``automation_reducer``): owns timer dequeue,
  timer touch, and wakeup dispatch transitions.  Store methods delegate
  domain decisions here instead of inline mutation.
- **Adapters**: persistence (SQLite, JSON), transport (Discord, Telegram),
  and surface modules.  They consume domain types and execute side effects.
- **Surfaces**: CLI, web routes, chat commands.  They call adapters.

Every PMA routing, wakeup, or publish policy decision should live in this
package.  If you are tempted to add a routing branch or suppression check
in an adapter file, add it here instead.
"""
