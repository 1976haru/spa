from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from .service import CaptureService
from .batch import BatchSourcingService


def install_capture_routes(app, service: CaptureService | None = None) -> None:
    """Mount authenticated, loopback-only capture routes on the UI's FastAPI app."""
    capture = service or CaptureService()
    batches = BatchSourcingService(capture.db)

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
            result["batch"] = batches.attach_latest(body.get("store_id"), body.get("keyword", ""), result["run_id"])
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
        body = await request.json()
        run_id = body.get("batch_run_id")
        if body.get("event") == "CAPTCHA_DETECTED":
            batch = batches.pause_for_captcha(run_id) if run_id else batches.pause_latest_for_keyword(body.get("store_id", ""), body.get("keyword", ""))
            return {"status": "paused", "batch": batch}
        if body.get("event") == "DETAIL_CAPTURE_FAILED" and run_id:
            return {"status": "recorded", "batch": batches.record_detail(run_id, body.get("asin", ""), "FAILED", body.get("reason", "capture failed"))}
        if run_id and body.get("event") == "NEXT_ITEM":
            return {"status": "ok", "item": batches.next_item(run_id)}
        return {"status": "ok"}

    async def batch_status(request: Request):
        denied = await authorized(request)
        if denied: return denied
        run_id = request.path_params["run_id"]
        try: return batches.get(run_id)
        except KeyError: return JSONResponse({"error": "Batch not found."}, status_code=404)

    async def batch_action(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try:
            body = await request.json()
            return batches.action(request.path_params["run_id"], body.get("action", ""))
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def batch_create(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try:
            body = await request.json()
            return batches.create(body.get("store_id", ""), body.get("keyword", ""), body.get("target_candidates", 50), body.get("auto_import_master", True), body.get("target_mode", "CANDIDATES"))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    for path, endpoint, methods in (
        ("/api/capture/health", health, ["GET"]),
        ("/api/capture/search-results", search_results, ["POST"]),
        ("/api/capture/product-detail", product_detail, ["POST"]),
        ("/api/capture/heartbeat", heartbeat, ["POST"]),
        ("/api/capture/batches", batch_create, ["POST"]),
        ("/api/capture/batches/{run_id}", batch_status, ["GET"]),
        ("/api/capture/batches/{run_id}/action", batch_action, ["POST"]),
    ):
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
