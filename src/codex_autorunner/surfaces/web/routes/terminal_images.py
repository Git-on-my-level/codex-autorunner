"""
Terminal image upload routes.
"""

import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
ALLOWED_EXTS = set(ALLOWED_CONTENT_TYPES.values())


def _choose_image_extension(
    filename: Optional[str], content_type: Optional[str]
) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in ALLOWED_EXTS:
            return suffix
    if content_type:
        mapped = ALLOWED_CONTENT_TYPES.get(content_type.lower())
        if mapped:
            return mapped
    return ".img"


def build_terminal_image_routes() -> APIRouter:
    router = APIRouter()

    def _allow_abs_paths(request: Request, include_abs_paths: bool) -> bool:
        if not include_abs_paths:
            return False
        return bool(getattr(request.app.state, "auth_token", None))

    @router.post("/api/terminal/image")
    async def upload_terminal_image(
        request: Request,
        file: UploadFile = File(...),
        include_abs_paths: bool = False,
    ):
        if not file:
            raise HTTPException(status_code=400, detail="missing image")

        content_type = (file.content_type or "").lower()
        content_type = content_type.split(";", 1)[0].strip()
        filename = file.filename or ""
        suffix = Path(filename).suffix.lower()
        looks_like_image = False
        if content_type.startswith("image/"):
            looks_like_image = True
        elif suffix in ALLOWED_EXTS:
            looks_like_image = True
        if not looks_like_image:
            raise HTTPException(status_code=400, detail="unsupported content type")

        try:
            data = await file.read()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="unable to read upload"
            ) from exc

        if not data:
            raise HTTPException(status_code=400, detail="empty upload")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image too large")

        engine = request.app.state.engine
        repo_root = Path(engine.repo_root)
        images_dir = repo_root / ".codex-autorunner" / "uploads" / "terminal-images"
        images_dir.mkdir(parents=True, exist_ok=True)

        ext = _choose_image_extension(filename, content_type)
        token = secrets.token_hex(6)
        name = f"terminal-{int(time.time())}-{token}{ext}"
        path = images_dir / name
        try:
            path.write_bytes(data)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="failed to save image") from exc

        rel_path = path.relative_to(repo_root).as_posix()
        payload = {
            "status": "ok",
            "path": rel_path,
            "filename": name,
        }
        if _allow_abs_paths(request, include_abs_paths):
            payload["abs_path"] = str(path)
        return payload

    return router


__all__ = ["build_terminal_image_routes"]
