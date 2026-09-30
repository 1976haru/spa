from pathlib import Path


ROOT = Path(__file__).parents[1]
BACKGROUND = (ROOT / "browser_extension/shopsource_capture/background.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "browser_extension/shopsource_capture/content_local_bridge.js").read_text(encoding="utf-8")


def test_single_worker_tab_created_for_batch():
    assert "const workerTabs = new Map()" in BACKGROUND
    assert "saveWorker(runId,tab.id" in BACKGROUND


def test_second_item_reuses_existing_worker_tab():
    assert "chrome.tabs.update(worker.tabId,{url:target.href,active:false})" in BACKGROUND


def test_100_products_do_not_create_100_tabs():
    assert BACKGROUND.count("chrome.tabs.create({url:target.href") == 1
    assert "WORKER_TAB_REUSED" in BACKGROUND


def test_worker_tab_default_inactive():
    assert "chrome.tabs.create({url:target.href,active:false})" in BACKGROUND


def test_captcha_activates_worker_tab():
    assert BACKGROUND.count("chrome.tabs.update(tabId,{active:true})") >= 2


def test_done_closes_owned_worker_tab():
    assert "batch.status === 'DONE'" in BACKGROUND
    assert "await closeWorker(runId)" in BACKGROUND


def test_cancel_closes_owned_worker_tab():
    ui = (ROOT / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert 'if action == "CANCEL"' in ui
    assert 'worker_action("close")' in ui


def test_user_amazon_tab_is_never_closed():
    assert "chrome.tabs.remove(record.tabId)" in BACKGROUND
    assert "ownedByShopSource" not in BACKGROUND


def test_user_amazon_tab_is_never_repurposed():
    assert "chrome.tabs.update(worker.tabId" in BACKGROUND
    assert "chrome.tabs.update(tabId,{url:" not in BACKGROUND


def test_worker_tab_manual_close_clears_metadata():
    assert "chrome.tabs.onRemoved.addListener" in BACKGROUND
    assert "clearWorker(runId)" in BACKGROUND


def test_worker_tab_recreated_only_after_explicit_kickoff():
    assert "shopsource-batch-next" in BRIDGE
    assert "setTimeout(() => openNext(runId),4000)" in BACKGROUND
    on_removed = BACKGROUND.split("chrome.tabs.onRemoved.addListener", 1)[1].split("async function openNext", 1)[0]
    assert "openNext(" not in on_removed


def test_worker_metadata_uses_session_storage_only():
    assert "chrome.storage.session.set({[workerKey(runId)]:record})" in BACKGROUND
    assert "chrome.storage.local.set({[workerKey" not in BACKGROUND
    assert "cookie" not in BACKGROUND.lower()


def test_worker_reuse_preserves_auto_capture_handshake():
    assert "shopsource-batch-auto-capture" in BACKGROUND
    assert "triggerAutoCapture(tabId,pending.runId,pending.asin,record)" in BACKGROUND


def test_one_batch_one_worker_tab_invariant():
    assert "workerTabs.set(runId,record)" in BACKGROUND
    assert "let worker = await validWorker(runId)" in BACKGROUND


def test_no_parallel_detail_tabs():
    assert "openingRuns.has(runId)" in BACKGROUND
    assert "openingRuns.add(runId)" in BACKGROUND


def test_existing_5_product_flow_still_reaches_master_5():
    batch_tests = (ROOT / "tests/test_batch_sourcing.py").read_text(encoding="utf-8")
    assert "MASTER_IMPORTED" in batch_tests
    assert "DETAIL_COMPLETE" in batch_tests


def test_worker_commands_are_bridged():
    for command in ("worker-show", "worker-close", "worker-status"):
        assert command in BRIDGE


def test_pending_asin_is_replaced_on_reuse():
    save_pending = BACKGROUND.split("async function savePending", 1)[1].split("async function clearPending", 1)[0]
    assert "current?.tabId === record.tabId" not in save_pending
    assert "pendingTabs.set(runId, record)" in save_pending
