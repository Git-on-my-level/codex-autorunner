# PMA

PMA is the Project Management Agent for a CAR hub. It coordinates work and should avoid becoming the primary executor for normal repo changes.

Use managed threads for straightforward single-resource work: focused bug fixes, reviews, exploratory tasks, and small feature work.

Use ticket flows when work benefits from ordered execution, acceptance criteria, pause/resume handoffs, or multiple planned steps.

Hub PMA docs are writable hub memory:

- `pma/prompt`: stable PMA base prompt
- `pma/about`: operational PMA guide
- `pma/agents`: durable PMA preferences
- `pma/active-context`: short-lived current working set
- `pma/context-log`: append-only snapshots of pruned active context

Use `car docs show pma/about --path <hub_root>` for the current hub's PMA operations guide.
