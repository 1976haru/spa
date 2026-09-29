# ShopSource Capture (Edge / Chrome)

Unpacked Manifest V3 extension for explicitly capturing product information currently visible in an Amazon.com tab. It reads visible result cards or product detail DOM/JSON-LD only and sends the selected page data to `http://127.0.0.1:8081` with the local pairing header. It never reads cookies, Web Storage, account data, or authorization values and does not contact any third-party service.

1. Start ShopSource Studio UI V2 and create a pairing code in Settings → Browser Capture.
2. Open `edge://extensions` (Edge) or `chrome://extensions` (Chrome), enable Developer mode, and choose **Load unpacked**.
3. Select this `shopsource_capture` folder.
4. Open extension Options and enter the local URL, Store ID, and pairing code.
5. On an Amazon search page or product page, use the ShopSource page button or extension popup.

The extension captures only the current visible search-results page. It does not paginate, bypass CAPTCHA/robot checks, or automatically traverse products. Detail tabs are user-started. Image URLs are recorded; image binaries are not downloaded.
