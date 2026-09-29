from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from .service import CaptureService


def install_capture_routes(app, service: CaptureService | None = None) -> None:
    """Mount authenticated, loopback-only capture routes on the UI's FastAPI app."""
    capture = service or CaptureService()

    async def authorized(request: Request):
        token = request.headers.get("x-shopsource-pairing", "")
        if not capture.authenticate(token):
            return JSONResponse({"error": "Pairing code가 맞지 않습니다."}, status_code=401)
        return None

    async def health(request: Request):
        denied = await authorized(request)
        if denied:
            return denied
        return {"status": "ok", "service": "ShopSource Studio", "capture": True}

    async def search_results(request: Request):
        denied = await authorized(request)
        if denied:
            return denied
        try:
            body = await request.json()
            result = capture.capture_search(body)
            return result
        except Exception as exc:
            capture.log_error("SEARCH", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def product_detail(request: Request):
        denied = await authorized(request)
        if denied:
            return denied
        try:
            body = await request.json()
            result = capture.capture_detail(body)
            return result
        except Exception as exc:
            capture.log_error("DETAIL", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def heartbeat(request: Request):
        denied = await authorized(request)
        if denied:
            return denied
        return {"status": "ok"}

    for path, endpoint, methods in (
        ("/api/capture/health", health, ["GET"]),
        ("/api/capture/search-results", search_results, ["POST"]),
        ("/api/capture/product-detail", product_detail, ["POST"]),
        ("/api/capture/heartbeat", heartbeat, ["POST"]),
    ):
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
