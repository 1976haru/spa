from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from .service import CaptureService
from .batch import BatchSourcingService
from .campaign import CampaignService


def install_capture_routes(app, service: CaptureService | None = None) -> None:
    """Mount authenticated, loopback-only capture routes on the UI's FastAPI app."""
    capture = service or CaptureService()
    batches = BatchSourcingService(capture.db)
    campaigns = CampaignService(capture.db)

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
            campaign_id = str(body.get("campaign_id") or "")
            if campaign_id:
                result["campaign"] = campaigns.record_search_capture(
                    campaign_id, result["run_id"], body.get("next_url"), bool(body.get("exhausted")))
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
        if body.get("event") == "EXTENSION_EVENT" and run_id:
            try:
                batch = batches.record_extension_event(
                    run_id,
                    body.get("event_name", ""),
                    body.get("asin", ""),
                    body.get("reason", ""),
                    body.get("tab_id"),
                )
                return {"status": "recorded", "batch": batch}
            except (KeyError, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
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

    async def campaign_create(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try:
            body = await request.json()
            return campaigns.create_live_2000(body.get("store_id", ""), body.get("target", 2000), body.get("search_delay_seconds", 8), body.get("detail_interval_seconds", 4))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def campaign_status(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try: return campaigns.get(request.path_params["campaign_id"])
        except KeyError: return JSONResponse({"error": "Campaign not found."}, status_code=404)

    async def campaign_action(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try:
            body = await request.json()
            campaign_id = request.path_params["campaign_id"]
            campaign = campaigns.action(campaign_id, body.get("action", ""))
            campaign["search_instruction"] = campaigns.next_search(campaign_id)
            return campaign
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    async def campaign_event(request: Request):
        denied = await authorized(request)
        if denied: return denied
        try:
            body = await request.json()
            return campaigns.record_extension_event(request.path_params["campaign_id"], body.get("event", ""), body.get("payload", {}))
        except KeyError: return JSONResponse({"error": "Campaign not found."}, status_code=404)
        except ValueError as exc: return JSONResponse({"error": str(exc)}, status_code=422)

    for path, endpoint, methods in (
        ("/api/capture/health", health, ["GET"]),
        ("/api/capture/search-results", search_results, ["POST"]),
        ("/api/capture/product-detail", product_detail, ["POST"]),
        ("/api/capture/heartbeat", heartbeat, ["POST"]),
        ("/api/capture/batches", batch_create, ["POST"]),
        ("/api/capture/batches/{run_id}", batch_status, ["GET"]),
        ("/api/capture/batches/{run_id}/action", batch_action, ["POST"]),
        ("/api/capture/campaigns", campaign_create, ["POST"]),
        ("/api/capture/campaigns/{campaign_id}", campaign_status, ["GET"]),
        ("/api/capture/campaigns/{campaign_id}/action", campaign_action, ["POST"]),
        ("/api/capture/campaigns/{campaign_id}/events", campaign_event, ["POST"]),
    ):
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
