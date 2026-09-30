# ShopSource Capture (Edge / Chrome)

Batch detail pages are started only from a ShopSource batch and opened sequentially, one tab at a time. The `tabs` permission is used only to create the next validated `https://www.amazon.com/` detail URL; no tabs are opened for search pagination. A `#shopsource_capture=<run_id>` URL fragment triggers one-shot detail capture and is removed from the visible URL after success. CAPTCHA/robot-check pages pause the run for direct user action.

Unpacked Manifest V3 extension for explicitly capturing product information present in an Amazon.com tab. Search capture reads qualifying rendered result cards from the current page DOM regardless of viewport position; lazy/unrendered cards are not inferred. It sends selected page data to `http://127.0.0.1:8081` with the local pairing header. It never reads cookies, Web Storage, account data, or authorization values and does not contact any third-party service.

1. Start ShopSource Studio UI V2 and create a pairing code in Settings → Browser Capture.
2. Open `edge://extensions` (Edge) or `chrome://extensions` (Chrome), enable Developer mode, and choose **Load unpacked**.
3. Select this `shopsource_capture` folder.
4. Open extension Options and enter the local URL, Store ID, and pairing code.
5. On an Amazon search page or product page, use the ShopSource page button or extension popup.

The extension captures only the current search-results page DOM. It does not paginate or bypass CAPTCHA/robot checks. Detail tabs are started by an explicitly created ShopSource batch and processed sequentially, one at a time. Image URLs are recorded; image binaries are not downloaded.
