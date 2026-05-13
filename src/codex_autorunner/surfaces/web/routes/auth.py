from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..services.browser_auth import (
    SESSION_COOKIE_NAME,
    BrowserAuthStore,
)


class BootstrapClaimRequest(BaseModel):
    token: str


def _bootstrap_html() -> str:
    return r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>CAR Bootstrap</title>
    <style>
      body {
        background: #0a0c12;
        color: #f3f7fb;
        font: 16px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
      }
      main {
        width: min(32rem, calc(100vw - 2rem));
      }
      h1 {
        font-size: 1.5rem;
        font-weight: 650;
        margin: 0 0 0.75rem;
      }
      p {
        color: #b7c3d2;
        line-height: 1.5;
        margin: 0.5rem 0;
      }
      code {
        color: #6cf5d8;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>CAR Bootstrap</h1>
      <p id="status">Claiming browser session...</p>
      <p id="detail"></p>
    </main>
    <script>
      (async function () {
        var status = document.getElementById('status');
        var detail = document.getElementById('detail');
        var params = new URLSearchParams((window.location.hash || '').replace(/^#/, ''));
        var token = params.get('token') || '';
        if (!token) {
          status.textContent = 'Missing bootstrap token.';
          detail.innerHTML = 'Open this page as <code>/auth/bootstrap#token=...</code>.';
          return;
        }
        window.history.replaceState(null, document.title, window.location.pathname + window.location.search);
        try {
          var response = await fetch(window.location.pathname.replace(/\/$/, '') + '/claim', {
            method: 'POST',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ token: token })
          });
          if (!response.ok) throw new Error('Bootstrap claim failed.');
          window.location.replace(window.location.pathname.replace(/\/auth\/bootstrap\/?$/, '/') || '/');
        } catch (error) {
          status.textContent = 'Bootstrap claim failed.';
          detail.textContent = error && error.message ? error.message : 'Request failed.';
        }
      })();
    </script>
  </body>
</html>
"""


def build_auth_routes(store: BrowserAuthStore) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/bootstrap", include_in_schema=False)
    def bootstrap_page() -> HTMLResponse:
        return HTMLResponse(
            _bootstrap_html(),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
                ),
            },
        )

    @router.post("/auth/bootstrap/claim", include_in_schema=False)
    def claim_bootstrap(
        payload: BootstrapClaimRequest, request: Request
    ) -> JSONResponse:
        claim = store.claim_bootstrap_token(payload.token)
        if claim is None:
            raise HTTPException(status_code=401, detail="Invalid bootstrap token")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE_NAME,
            claim.session_token,
            max_age=claim.max_age_seconds,
            httponly=True,
            secure=True,
            samesite="lax",
            path=request.scope.get("root_path") or "/",
        )
        return response

    return router
