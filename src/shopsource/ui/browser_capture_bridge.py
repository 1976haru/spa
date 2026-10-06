"""Small, shared bridge to the installed Browser Capture extension."""

from __future__ import annotations

import json
import secrets
import asyncio


def _command_script(message_type: str, run_id: str, request_id: str, wait: bool) -> str:
    message = {
        "source": "shopsource-studio-ui",
        "type": message_type,
        "runId": str(run_id),
        "requestId": request_id,
    }
    serialized = json.dumps(message, separators=(",", ":"))
    if not wait:
        return f"(() => {{ window.postMessage({serialized}, window.location.origin); return {{state:'REQUESTED'}}; }})()"
    response_type = "batch-open-result" if message_type == "batch-open-next" else "worker-command-result"
    return f"""(async () => await new Promise(resolve => {{
      const request = {serialized};
      let finished = false;
      const done = value => {{ if (finished) return; finished = true; clearTimeout(timer); window.removeEventListener('message', listener); resolve(value); }};
      const listener = event => {{
        const data = event.data || {{}};
        if (event.source !== window || event.origin !== window.location.origin || data.source !== 'shopsource-capture-extension' || data.type !== {json.dumps(response_type)} || data.requestId !== request.requestId) return;
        done({{state: data.ok ? 'ACKNOWLEDGED' : 'EXTENSION_ERROR', error: data.error || ''}});
      }};
      const timer = setTimeout(() => done({{state:'EXTENSION_NOT_CONNECTED'}}), 1800);
      window.addEventListener('message', listener);
      window.postMessage(request, window.location.origin);
    }}))()"""


async def request_extension(ui, message_type: str, run_id: str, *, wait: bool = True) -> dict:
    """Send a command through the existing local bridge and optionally await its ack."""
    request_id = "BCUI_" + secrets.token_hex(8)
    script = _command_script(message_type, run_id, request_id, wait)
    result = await ui.run_javascript(script, timeout=3.0)
    if not isinstance(result, dict):
        return {"state": "EXTENSION_NOT_CONNECTED" if wait else "REQUESTED"}
    return result


async def request_batch_open_next(ui, run_id: str) -> dict:
    return await request_extension(ui, "batch-open-next", run_id)


async def start_free_capture(runner, production_run_id: str, batch_kind: str, ui) -> dict:
    """Reuse/create the local queue, resume it, then ask the existing extension for one item."""
    batch = await asyncio.to_thread(runner.prepare_free_browser_capture_batch,
        production_run_id, batch_kind, confirmed=True)
    browser_run_id = batch["browser_batch_run_id"]
    if batch.get("browser_status") == "DONE_WITH_ERRORS" and (batch.get("browser_batch") or {}).get("failed_count", 0):
        from ..capture.batch import BatchSourcingService
        await asyncio.to_thread(BatchSourcingService(runner.db).action, browser_run_id, "RETRY")
    elif batch.get("browser_status") != "RUNNING":
        await asyncio.to_thread(runner.resume_free_browser_capture_batch, browser_run_id)
    bridge = await request_batch_open_next(ui, browser_run_id)
    return {"batch": batch, "bridge": bridge, "browser_batch_run_id": browser_run_id}


async def request_worker_show(ui, run_id: str) -> dict:
    return await request_extension(ui, "worker-show", run_id)


async def request_worker_status(ui, run_id: str) -> dict:
    return await request_extension(ui, "worker-status", run_id)


async def request_worker_close(ui, run_id: str) -> dict:
    return await request_extension(ui, "worker-close", run_id)


def post_batch_open_next(ui, run_id: str) -> None:
    """Fire-and-forget variant used by the legacy /sourcing controls."""
    request_id = "BCUI_" + secrets.token_hex(8)
    ui.run_javascript(_command_script("batch-open-next", run_id, request_id, False))
