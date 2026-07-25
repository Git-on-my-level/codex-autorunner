"""Registry of orchestration migrations beyond the frozen v1-46 ladder.

Rule: v1-46 are frozen in ``migrations.py`` (``_apply_v1`` through
``_apply_v46``, and the ``_MIGRATIONS`` tuple). They must never be modified,
renumbered, reordered, or merged — behavior against existing databases must
stay bit-identical. Every new durable-state migration is v47 or higher and
is added here instead of to ``_MIGRATIONS``.

How to add a new migration:

1. Create ``migrations_future/v047_<slug>.py`` exporting a single
   ``STEP: SqliteMigrationStep`` (see that package's docstring for the
   template, and ``migration_sqlite_helpers.py`` for reusable column/backfill
   helpers).
2. Import its ``STEP`` here and append it to ``REGISTERED_MIGRATIONS`` below,
   in ascending version order, continuing from 47.

``migrations.py`` is the single ordering authority for applying migrations:
it always runs the frozen v1-46 ladder first (in its existing order), then
runs ``REGISTERED_MIGRATIONS`` in the order they appear here.
``ORCHESTRATION_SCHEMA_VERSION`` is derived from the combined list, so
registering a step here automatically becomes the new target schema version.
"""

from __future__ import annotations

from ..sqlite_utils import SqliteMigrationStep

# Import and append new migration steps here, in ascending version order,
# e.g.:
#
#     from .migrations_future.v047_add_widget_table import STEP as _V047
#
#     REGISTERED_MIGRATIONS: tuple[SqliteMigrationStep, ...] = (_V047,)

REGISTERED_MIGRATIONS: tuple[SqliteMigrationStep, ...] = ()

__all__ = ["REGISTERED_MIGRATIONS"]
