from __future__ import annotations

import base64
import binascii
import hmac
import logging
import time
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi.responses import RedirectResponse, Response

from ...core.config import _normalize_base_path
from ...core.logging_utils import log_event
from ...core.request_context import reset_request_id, set_request_id
from .static_assets import security_headers

logger = logging.getLogger("codex_autorunner.web.middleware")


class BasePathRouterMiddleware:
    """
    Middleware that keeps the app mounted at / while enforcing a canonical base path.
    - Requests that already include the base path are routed via root_path so routing stays rooted at /.
    - Requests missing the base path but pointing at known CAR prefixes are redirected to the
      canonical location (HTTP 308). WebSocket handshakes get the same redirect response.
    """

    def __init__(self, app, base_path: str, known_prefixes=None):
        self.app = app
        self.base_path = _normalize_base_path(base_path)
        self.base_path_bytes = self.base_path.encode("utf-8")
        self.known_prefixes = tuple(
            known_prefixes
            or (
                "/",
                "/api",
                "/hub",
                "/repos",
                "/static",
                "/health",
                "/cat",
            )
        )

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _has_base(self, path: str, root_path: str) -> bool:
        if not self.base_path:
            return True
        full_path = f"{root_path}{path}" if root_path else path
        if full_path == self.base_path or full_path.startswith(f"{self.base_path}/"):
            return True
        return path == self.base_path or path.startswith(f"{self.base_path}/")

    def _should_redirect(self, path: str, root_path: str) -> bool:
        if not self.base_path:
            return False
        if self._has_base(path, root_path):
            return False
        return any(
            path == prefix
            or path.startswith(f"{prefix}/")
            or (root_path and root_path.startswith(prefix))
            for prefix in self.known_prefixes
        )

    async def _redirect(self, scope, receive, send, target: str):
        if scope["type"] == "websocket":
            headers = [(b"location", target.encode("utf-8"))]
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 308,
                    "headers": headers,
                }
            )
            await send(
                {
                    "type": "websocket.http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
            return
        response = RedirectResponse(target, status_code=308)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path") or "/"
        root_path = scope.get("root_path") or ""

        if not self.base_path:
            return await self.app(scope, receive, send)

        if self._has_base(path, root_path):
            scope = dict(scope)
            # Preserve the base path for downstream routing + URL generation.
            if not root_path:
                scope["root_path"] = self.base_path
                root_path = self.base_path

            # Starlette expects scope["path"] to include scope["root_path"] for
            # mounted sub-apps (including /repos/* and /static/*). If we detect
            # an already-stripped path (e.g., behind a proxy), re-prefix it.
            if root_path and not path.startswith(root_path):
                if path == "/":
                    scope["path"] = root_path
                else:
                    scope["path"] = f"{root_path}{path}"
                raw_path = scope.get("raw_path")
                if raw_path and not raw_path.startswith(self.base_path_bytes):
                    if raw_path == b"/":
                        scope["raw_path"] = self.base_path_bytes
                    else:
                        scope["raw_path"] = self.base_path_bytes + raw_path
            return await self.app(scope, receive, send)

        if self._should_redirect(path, root_path):
            target_path = f"{self.base_path}{path}"
            query_string = scope.get("query_string") or b""
            if query_string:
                target_path = f"{target_path}?{query_string.decode('latin-1')}"
            if not target_path:
                target_path = "/"
            return await self._redirect(scope, receive, send, target_path)

        return await self.app(scope, receive, send)


class AuthTokenMiddleware:
    """Middleware that enforces an auth token on all non-public endpoints."""

    def __init__(self, app, token: str, base_path: str = ""):
        self.app = app
        self.token = token
        self.base_path = _normalize_base_path(base_path)
        self.public_prefixes = ("/static", "/health", "/cat")

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _full_path(self, scope) -> str:
        path = scope.get("path") or "/"
        root_path = scope.get("root_path") or ""
        if root_path and path.startswith(root_path):
            return path
        if root_path:
            return f"{root_path}{path}"
        return path

    def _strip_base_path(self, path: str) -> str:
        if self.base_path and path.startswith(self.base_path):
            stripped = path[len(self.base_path) :]
            return stripped or "/"
        return path

    def _strip_repo_mount(self, path: str) -> str:
        if not path.startswith("/repos/"):
            return path
        parts = path.split("/", 3)
        if len(parts) < 4:
            return path
        if not parts[3]:
            return path
        remainder = f"/{parts[3]}"
        return remainder or "/"

    def _is_public_path(self, path: str) -> bool:
        if path == "/":
            return True
        for prefix in self.public_prefixes:
            if path == prefix or path.startswith(f"{prefix}/"):
                return True
        return False

    def _requires_auth(self, scope) -> bool:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            return False
        full_path = self._strip_base_path(self._full_path(scope))
        repo_path = self._strip_repo_mount(full_path)
        return not self._is_public_path(repo_path)

    def _extract_header_token(self, scope) -> str | None:
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        raw = headers.get(b"authorization")
        if not raw:
            return None
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not value.lower().startswith("bearer "):
            return None
        return value.split(" ", 1)[1].strip() or None

    def _extract_query_token(self, scope) -> str | None:
        query_string = scope.get("query_string") or b""
        if not query_string:
            return None
        parsed = parse_qs(query_string.decode("latin-1"))
        token_values = parsed.get("token") or []
        return token_values[0] if token_values else None

    def _extract_ws_protocol_token(self, scope) -> str | None:
        if scope.get("type") != "websocket":
            return None
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        raw = headers.get(b"sec-websocket-protocol")
        if not raw:
            return None
        try:
            value = raw.decode("latin-1")
        except UnicodeDecodeError:
            return None
        for entry in value.split(","):
            candidate = entry.strip()
            if candidate.startswith("car-token-b64."):
                token = candidate[len("car-token-b64.") :].strip()
                if not token:
                    continue
                padding = "=" * (-len(token) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(f"{token}{padding}")
                except (binascii.Error, ValueError):
                    logger.debug("Failed to decode base64 token")
                    continue
                try:
                    return decoded.decode("utf-8").strip() or None
                except UnicodeDecodeError:
                    continue
            if candidate.startswith("car-token."):
                token = candidate[len("car-token.") :].strip()
                if token:
                    return token
        return None

    async def _reject_http(self, scope, receive, send) -> None:
        response = Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)

    async def _reject_ws(self, scope, receive, send) -> None:
        await send({"type": "websocket.close", "code": 1008})

    async def __call__(self, scope, receive, send):
        if not self._requires_auth(scope):
            return await self.app(scope, receive, send)

        token = self._extract_header_token(scope)
        if token is None:
            if scope.get("type") == "websocket":
                token = self._extract_ws_protocol_token(scope)
            token = token or self._extract_query_token(scope)

        if not token or not hmac.compare_digest(token, self.token):
            if scope.get("type") == "websocket":
                return await self._reject_ws(scope, receive, send)
            return await self._reject_http(scope, receive, send)

        return await self.app(scope, receive, send)


class HostOriginMiddleware:
    """Validate Host and Origin headers for localhost hardening."""

    def __init__(self, app, allowed_hosts, allowed_origins):
        self.app = app
        self.allowed_hosts = [
            entry.strip().lower()
            for entry in (allowed_hosts or [])
            if isinstance(entry, str) and entry.strip()
        ]
        self.allowed_origins = {
            entry
            for entry in (
                self._normalize_origin(raw)
                for raw in (allowed_origins or [])
                if isinstance(raw, str) and raw.strip()
            )
            if entry is not None
        }

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _header(self, scope, key: bytes) -> str | None:
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        raw = headers.get(key)
        if not raw:
            return None
        try:
            return raw.decode("latin-1")
        except Exception:
            return None

    def _split_host_port(self, value: str) -> tuple[str, str | None]:
        value = value.strip().lower()
        if not value:
            return "", None
        if value.startswith("["):
            end = value.find("]")
            if end != -1:
                host = value[1:end]
                rest = value[end + 1 :]
                if rest.startswith(":") and len(rest) > 1:
                    return host, rest[1:]
                return host, None
        if value.count(":") == 1:
            host, port = value.rsplit(":", 1)
            if host and port:
                return host, port
        return value, None

    def _host_allowed(self, host_header: str | None) -> bool:
        if not self.allowed_hosts:
            return True
        if not host_header:
            return False
        header_host, header_port = self._split_host_port(host_header)
        for allowed in self.allowed_hosts:
            if allowed == "*":
                return True
            allowed_host, allowed_port = self._split_host_port(allowed)
            if allowed_host != header_host:
                continue
            if allowed_port is None or allowed_port == header_port:
                return True
        return False

    def _normalize_origin(self, origin: str) -> str | None:
        value = origin.strip().lower()
        if not value:
            return None
        if value == "null":
            return value
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return value

    def _origin_scheme(self, scheme: str) -> str:
        if scheme == "ws":
            return "http"
        if scheme == "wss":
            return "https"
        return scheme

    def _request_origin(self, scheme: str, host_header: str | None) -> str | None:
        if not host_header:
            return None
        normalized_scheme = self._origin_scheme(scheme).lower()
        return f"{normalized_scheme}://{host_header.strip().lower()}"

    def _origin_allowed(
        self, origin: str | None, scheme: str, host: str | None
    ) -> bool:
        if not origin:
            return True
        normalized = self._normalize_origin(origin)
        if not normalized:
            return False
        if normalized in self.allowed_origins:
            return True
        request_origin = self._request_origin(scheme, host)
        return request_origin == normalized

    async def _reject_http(self, scope, receive, send, status: int, body: str) -> None:
        response = Response(content=body, status_code=status)
        await response(scope, receive, send)

    async def _reject_ws(self, send, status: int, body: str) -> None:
        await send(
            {
                "type": "websocket.http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send(
            {
                "type": "websocket.http.response.body",
                "body": body.encode("utf-8"),
                "more_body": False,
            }
        )

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        host = self._header(scope, b"host")
        if not self._host_allowed(host):
            if scope_type == "websocket":
                return await self._reject_ws(send, 400, "Invalid host")
            return await self._reject_http(scope, receive, send, 400, "Invalid host")

        origin = self._header(scope, b"origin")
        scheme = scope.get("scheme") or "http"
        if scope_type == "websocket":
            if origin and not self._origin_allowed(origin, scheme, host):
                return await self._reject_ws(send, 403, "Forbidden")
            return await self.app(scope, receive, send)

        method = (scope.get("method") or "GET").upper()
        if method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            if not self._origin_allowed(origin, scheme, host):
                return await self._reject_http(scope, receive, send, 403, "Forbidden")

        return await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Attach security headers to HTML responses."""

    def __init__(self, app):
        self.app = app
        self.headers = security_headers()

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {name.lower() for name, _ in headers}
                content_type = None
                for name, value in headers:
                    if name.lower() == b"content-type":
                        try:
                            content_type = value.decode("latin-1").lower()
                        except UnicodeDecodeError:
                            logger.debug("Failed to decode content-type header")
                            content_type = None
                        break
                if content_type and content_type.startswith("text/html"):
                    for name, value in self.headers.items():
                        key = name.lower().encode("latin-1")
                        if key in existing:
                            continue
                        headers.append(
                            (name.encode("latin-1"), value.encode("latin-1"))
                        )
                    message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, send_wrapper)


class RequestIdMiddleware:
    """Attach request ids and emit structured request logs with latency and response size tracking."""

    def __init__(self, app, header_name: str = "x-request-id"):
        self.app = app
        self.header_name = header_name.lower()
        self.header_bytes = self.header_name.encode("latin-1")

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _extract_request_id(self, scope) -> str:
        for name, value in scope.get("headers") or []:
            if name.lower() == self.header_bytes:
                try:
                    candidate = value.decode("utf-8").strip()
                except UnicodeDecodeError:
                    candidate = ""
                if candidate:
                    return candidate
        return uuid.uuid4().hex

    def _get_logger(self, scope) -> logging.Logger:
        app = scope.get("app")
        state = getattr(app, "state", None) if app else None
        logger = getattr(state, "logger", None)
        if isinstance(logger, logging.Logger):
            return logger
        return logging.getLogger("codex_autorunner.web")

    def _is_heavy_endpoint(self, path: str) -> bool:
        """Check if endpoint should log response size (docs, runs, hub repos)."""
        path_lower = path.lower()
        heavy_prefixes = (
            "/api/workspace",
            "/api/workspace/spec/ingest",
            "/api/file-chat",
            "/api/usage",
            "/hub/usage",
            "/hub/repos",
        )
        return any(path_lower.startswith(prefix) for prefix in heavy_prefixes)

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type != "http":
            return await self.app(scope, receive, send)

        request_id = self._extract_request_id(scope)
        token = set_request_id(request_id)
        logger = self._get_logger(scope)
        method = scope.get("method") or "GET"
        path = scope.get("path") or "/"
        client = scope.get("client")
        client_addr = None
        if client and len(client) >= 2:
            client_addr = f"{client[0]}:{client[1]}"
        start = time.monotonic()
        status_code = None
        response_size = 0
        should_log_size = self._is_heavy_endpoint(path)

        log_event(
            logger,
            logging.INFO,
            "http.request",
            method=method,
            path=path,
            client=client_addr,
        )

        async def send_wrapper(message):
            nonlocal status_code, response_size
            if message.get("type") == "http.response.start":
                status_code = message.get("status")
                headers = list(message.get("headers") or [])
                existing = {name.lower() for name, _ in headers}
                if self.header_bytes not in existing:
                    headers.append((self.header_bytes, request_id.encode("latin-1")))
                message["headers"] = headers
            elif message.get("type") == "http.response.body" and should_log_size:
                body = message.get("body") or b""
                if isinstance(body, (bytes, bytearray)):
                    response_size += len(body)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            fields = {
                "method": method,
                "path": path,
                "status": status_code or 500,
                "duration_ms": round(duration_ms, 2),
            }
            if should_log_size:
                fields["response_size"] = response_size
            log_event(
                logger,
                logging.ERROR,
                "http.response",
                exc=exc,
                **fields,
            )
            raise
        else:
            duration_ms = (time.monotonic() - start) * 1000
            fields = {
                "method": method,
                "path": path,
                "status": status_code or 200,
                "duration_ms": round(duration_ms, 2),
            }
            if should_log_size:
                fields["response_size"] = response_size
            log_event(
                logger,
                logging.INFO,
                "http.response",
                **fields,
            )
        finally:
            reset_request_id(token)
