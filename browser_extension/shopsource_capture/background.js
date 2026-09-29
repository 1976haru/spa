const API_DEFAULT = 'http://127.0.0.1:8081';

async function sendCapture(kind, payload) {
  const config = await chrome.storage.local.get(['bridgeUrl', 'pairingCode', 'storeId']);
  if (!config.pairingCode || !config.storeId) throw new Error('확장 설정에서 Pairing code와 Store ID를 입력하세요.');
  const base = (config.bridgeUrl || API_DEFAULT).replace(/\/$/, '');
  const route = kind === 'search' ? '/api/capture/search-results' : '/api/capture/product-detail';
  const response = await fetch(base + route, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-ShopSource-Pairing': config.pairingCode},
    body: JSON.stringify({...payload, store_id: config.storeId}),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `ShopSource 연결 오류 (${response.status})`);
  return body;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'shopsource-capture') {
    sendCapture(message.kind, message.payload)
      .then(result => sendResponse({ok: true, result}))
      .catch(error => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message?.type === 'shopsource-health') {
    chrome.storage.local.get(['bridgeUrl', 'pairingCode']).then(async config => {
      const url = (config.bridgeUrl || API_DEFAULT).replace(/\/$/, '') + '/api/capture/health';
      try {
        const response = await fetch(url, {headers: {'X-ShopSource-Pairing': config.pairingCode || ''}});
        sendResponse({ok: response.ok, status: response.status});
      } catch (_error) { sendResponse({ok: false, status: 0}); }
    });
    return true;
  }
});
