from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.surfaces.web import app as web_app_module


@pytest.fixture(autouse=True)
def _web_static_bundle_for_route_tests(monkeypatch, tmp_path: Path) -> None:
    """Use a tiny SPA bundle when ignored built assets are absent.

    Web route tests assert FastAPI routing and cache/header behavior. The real
    SvelteKit build output is intentionally gitignored, so clean CI checkouts
    need a deterministic test bundle without changing production behavior.
    """

    web_static_dir, context = web_app_module.resolve_web_static_dir()
    if (web_static_dir / "index.html").exists():
        if context is not None:
            context.close()
        return

    static_dir = tmp_path / "web_static"
    asset_dir = static_dir / "_app" / "immutable" / "entry"
    asset_dir.mkdir(parents=True)
    (asset_dir / "app.test.css").write_text(
        "body { color: #111; }\n",
        encoding="utf-8",
    )
    (asset_dir / "app.test.js").write_text(
        "globalThis.__carTestApp = true;\n",
        encoding="utf-8",
    )
    (asset_dir / "start.test.js").write_text(
        "globalThis.__carTestStart = true;\n",
        encoding="utf-8",
    )
    (static_dir / "_app" / "version.json").write_text(
        '{"version":"test"}\n',
        encoding="utf-8",
    )
    (static_dir / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Web Hub</title>
  <link rel="stylesheet" href="/_app/immutable/entry/app.test.css" />
  <link href="/_app/immutable/entry/start.test.js" rel="modulepreload" />
</head>
<body>
  <div id="app"></div>
  <script>globalThis.__carBootstrap = true; __sveltekit_test = true; import("/_app/immutable/entry/start.test.js");</script>
  <script type="module" src="/_app/immutable/entry/app.test.js"></script>
</body>
</html>
""",
        encoding="utf-8",
    )

    def _resolve_test_web_static_dir() -> tuple[Path, Optional[object]]:
        return static_dir, None

    monkeypatch.setattr(
        web_app_module,
        "resolve_web_static_dir",
        _resolve_test_web_static_dir,
    )
