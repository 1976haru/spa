# ShopSource Capture (Edge / Chrome)

Batch detail pages are started only from a ShopSource batch and opened sequentially, one tab at a time. A small content bridge on the exact local UI origin (`http://127.0.0.1:8081`) relays a user-started next-item request to the service worker; the background reserves the next item and creates/tracks the Amazon tab. The `tabs` permission is used only for validated `https://www.amazon.com/` detail tabs; no tabs are opened for search pagination. The background service worker tracks run ID, expected ASIN, and tab ID in `chrome.storage.session`, then sends an explicit auto-capture message when the final product page loads. The `#shopsource_capture=<run_id>` fragment is only a fallback/bootstrap marker, not the capture trigger. CAPTCHA/robot-check pages pause the run for direct user action.

Unpacked Manifest V3 extension for explicitly capturing product information present in an Amazon.com tab. Search capture reads qualifying rendered result cards from the current page DOM regardless of viewport position; lazy/unrendered cards are not inferred. It sends selected page data to `http://127.0.0.1:8081` with the local pairing header. It never reads cookies, Web Storage, account data, or authorization values and does not contact any third-party service.

1. Start ShopSource Studio UI V2 and create a pairing code in Settings → Browser Capture.
2. Open `edge://extensions` (Edge) or `chrome://extensions` (Chrome), enable Developer mode, and choose **Load unpacked**.
3. Select this `shopsource_capture` folder.
4. Open extension Options and enter the local URL, Store ID, and pairing code.
5. On an Amazon search page or product page, use the ShopSource page button or extension popup.

After installing Phase 2.6.4, reload ShopSource Capture from `chrome://extensions` or `edge://extensions`, then refresh the open ShopSource UI tab once so the localhost content bridge is injected. Session metadata contains only the batch run ID, tab ID, ASIN, and creation time; no page content or Amazon credentials are stored.

The extension captures only the current search-results page DOM. It does not paginate or bypass CAPTCHA/robot checks. Detail tabs are started by an explicitly created ShopSource batch and processed sequentially, one at a time. Image URLs are recorded; image binaries are not downloaded.
