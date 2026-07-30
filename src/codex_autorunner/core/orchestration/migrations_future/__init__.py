"""Home for individual v47+ orchestration migration modules.

The frozen v1-46 migration ladder lives entirely in
``orchestration/migrations.py`` and must never be modified, renumbered,
reordered, or merged.

Every new durable-state migration to ``orchestration.sqlite3`` goes here
instead, as its own small module:

    orchestration/migrations_future/v047_<slug>.py

Each module should export exactly one migration step, e.g.::

    from ...sqlite_utils import SqliteMigrationStep

    def _apply(conn):
        ...

    STEP = SqliteMigrationStep(47, "add_widget_table", _apply)

Then register it, in ascending version order, in
``orchestration/migrations_registry.py``:

    from .migrations_future.v047_add_widget_table import STEP as _V047

    REGISTERED_MIGRATIONS = (_V047,)

This package is intentionally named ``migrations_future`` rather than
``migrations`` to avoid colliding with the existing ``migrations.py`` module
in the same package (Python does not allow a module and a sub-package to
share a name within the same parent package).

Helpers useful for writing new migrations (column/backfill utilities) live
in ``orchestration/migration_sqlite_helpers.py``; generic table introspection
(``table_exists``, ``table_columns``) lives in ``core/sqlite_utils.py``.
"""

from __future__ import annotations
