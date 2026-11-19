from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path


def mount_spa(app: FastAPI, dist: str) -> None:
    """Serve the built React SPA from the API process (single-container deployment)."""
    root = Path(dist or ".")
    index_file = root / "index.html"
    if not dist or not index_file.is_file():
        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "docs", "openapi.json", "healthz")):
            raise HTTPException(404, "Not Found")
        candidate = root / full_path
        if candidate.is_file() and root in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(index_file)
