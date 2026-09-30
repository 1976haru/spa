from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "browser_extension" / "shopsource_capture"


def test_background_has_auto_capture_handshake():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "shopsource-batch-auto-capture" in source
    assert "chrome.tabs.sendMessage" in source
    assert "triggerAutoCapture(tabId,pending.runId,pending.asin,record)" in source


def test_local_ui_bridge_requests_background_open():
    source = (EXTENSION / "content_local_bridge.js").read_text(encoding="utf-8")
    manifest = (EXTENSION / "manifest.json").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8081" in source
    assert "event.source !== window" in source
    assert "shopsource-batch-next" in source
    assert "http://127.0.0.1:8081/*" in manifest
    assert "shopsource-studio-ui" in (ROOT / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")


def test_auto_capture_does_not_require_fragment():
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    content = (EXTENSION / "content_product.js").read_text(encoding="utf-8")
    assert "trackMarkedTab" in background
    assert "if (marker)" in content  # retained only as fallback
    assert "shopsource-batch-auto-capture" in content
    assert "runAutoCapture({batchRunId:message.runId,expectedAsin:message.expectedAsin})" in content


def test_content_message_uses_explicit_run_id():
    source = (EXTENSION / "content_product.js").read_text(encoding="utf-8")
    assert "batchRunId || marker || undefined" in source
    assert "batchRunId:message.runId" in source


def test_content_expected_asin_guard():
    source = (EXTENSION / "content_product.js").read_text(encoding="utf-8")
    assert "String(product.asin || '').toUpperCase() !== String(expectedAsin).toUpperCase()" in source
    assert "Opened product ASIN did not match queued ASIN" in source


def test_fragment_and_message_are_one_shot():
    source = (EXTENSION / "content_product.js").read_text(encoding="utf-8")
    assert "let autoCapturePromise = null" in source
    assert "if (autoCapturePromise) return autoCapturePromise" in source
    assert "runAutoCapture({batchRunId:marker})" in source


def test_pending_tab_session_storage_contract():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "chrome.storage.session.set" in source
    assert "chrome.storage.session.get(null)" in source
    assert "chrome.storage.session.remove" in source
    assert "runId,tabId:record.tabId,asin:record.asin,createdAt:record.createdAt" in source
    assert "cookie" not in source.lower()
    assert "restorePendingTabs()" in source


def test_tab_complete_triggers_auto_capture():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "changeInfo.status !== 'complete'" in source
    assert "triggerAutoCapture(tabId,pending.runId,pending.asin,record)" in source


def test_intermediate_redirect_does_not_fail_immediately():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "Intermediate redirect pages are allowed to finish" in source
    assert "if (!/^\\/(?:dp|gp\\/product)" in source


def test_handshake_receiver_retry_is_bounded():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "const HANDSHAKE_ATTEMPTS = 10" in source
    assert "const HANDSHAKE_INTERVAL_MS = 500" in source
    assert "attempt<HANDSHAKE_ATTEMPTS" in source


def test_handshake_success_clears_pending():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "if (response?.ok)" in source
    assert "await clearPending(runId)" in source
    assert "AUTO_CAPTURE_ACK" in source


def test_handshake_failure_records_reason():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "AUTO_CAPTURE_ERROR" in source
    assert "RECEIVER_NOT_READY" in source
    assert "DETAIL_CAPTURE_FAILED" in source


def test_extension_version_and_reload_guidance():
    import json

    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.1.3"
    assert "v0.1.3" in (ROOT / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")
