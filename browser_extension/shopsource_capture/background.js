const API_DEFAULT = 'http://127.0.0.1:8081';
const openingRuns = new Set();
const pendingTabs = new Map();

function clearPending(runId) {
  const record = pendingTabs.get(runId);
  if (record?.timer) clearTimeout(record.timer);
  pendingTabs.delete(runId);
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url && changeInfo.status !== 'complete') return;
  const entry = [...pendingTabs.entries()].find(([, value]) => value.tabId === tabId);
  if (!entry) return;
  const [runId, value] = entry;
  const url = changeInfo.url || tab.url || '';
  const title = tab.title || '';
  if (!/captcha|robot.?check|validatecaptcha|validate-captcha/i.test(`${url} ${title}`)) return;
  clearPending(runId);
  config().then(settings => api('/api/capture/heartbeat',{event:'CAPTCHA_DETECTED',batch_run_id:runId,store_id:settings.storeId,asin:value.asin})).catch(()=>{});
});

async function config() {
  const value = await chrome.storage.local.get(['bridgeUrl', 'pairingCode', 'storeId']);
  if (!value.pairingCode || !value.storeId) throw new Error('Set the ShopSource pairing code and Store ID in extension options.');
  return {...value, base:(value.bridgeUrl || API_DEFAULT).replace(/\/$/, '')};
}
async function api(path, body, method = 'POST') {
  const settings = await config();
  const response = await fetch(settings.base + path, {method, headers:{'Content-Type':'application/json','X-ShopSource-Pairing':settings.pairingCode}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `ShopSource bridge error (${response.status})`);
  return result;
}
async function sendCapture(kind, payload) {
  const settings = await config();
  const route = kind === 'search' ? '/api/capture/search-results' : '/api/capture/product-detail';
  const response = await fetch(settings.base + route, {method:'POST',headers:{'Content-Type':'application/json','X-ShopSource-Pairing':settings.pairingCode},body:JSON.stringify({...payload,store_id:settings.storeId})});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `ShopSource bridge error (${response.status})`);
  return body;
}
async function openNext(runId) {
  if (!runId || openingRuns.has(runId)) return;
  openingRuns.add(runId);
  let reserved = null;
  try {
    const result = await api('/api/capture/heartbeat',{event:'NEXT_ITEM',batch_run_id:runId});
    const item = result.item;
    reserved = item;
    if (item?.url && /^https:\/\/(?:www\.)?amazon\.com\//i.test(item.url)) {
      const target = new URL(item.url);
      target.hash = `shopsource_capture=${encodeURIComponent(runId)}`;
      const tab = await chrome.tabs.create({url:target.href,active:true});
      clearPending(runId);
      const timer = setTimeout(async () => {
        pendingTabs.delete(runId);
        try {
          await api('/api/capture/heartbeat',{event:'DETAIL_CAPTURE_FAILED',batch_run_id:runId,asin:item.asin,reason:'Detail page capture timed out'});
          setTimeout(() => openNext(runId),4000);
        } catch (_error) {}
      },45000);
      pendingTabs.set(runId,{tabId:tab.id,asin:item.asin,timer});
    }
  } catch (_error) {
    if (reserved?.asin) {
      try { await api('/api/capture/heartbeat',{event:'DETAIL_CAPTURE_FAILED',batch_run_id:runId,asin:reserved.asin,reason:'Could not open the detail tab'}); }
      catch (_ignored) { /* persisted state remains available for operator recovery */ }
    }
  } finally { openingRuns.delete(runId); }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'shopsource-capture') {
    sendCapture(message.kind,message.payload).then(async result => {
      sendResponse({ok:true,result});
      const runId = message.kind === 'search' ? result.batch?.run_id : result.batch?.run_id;
      if (message.kind === 'detail' && runId) clearPending(runId);
      if (runId && message.kind === 'search') openNext(runId);
      if (runId && message.kind === 'detail') setTimeout(() => openNext(runId),4000);
    }).catch(error => sendResponse({ok:false,error:error.message}));
    return true;
  }
  if (message?.type === 'shopsource-batch-next') { openNext(message.runId); return false; }
  if (message?.type === 'shopsource-batch-captcha' || message?.type === 'shopsource-batch-failed') {
    const event = message.type === 'shopsource-batch-captcha' ? 'CAPTCHA_DETECTED' : 'DETAIL_CAPTURE_FAILED';
    if (message.runId) clearPending(message.runId);
    config().then(settings => api('/api/capture/heartbeat',{event,batch_run_id:message.runId || '',store_id:settings.storeId,asin:message.asin || '',keyword:message.keyword || '',reason:message.reason || ''})).then(() => {
      if (event === 'DETAIL_CAPTURE_FAILED' && message.runId) setTimeout(() => openNext(message.runId),4000);
    }).catch(()=>{});
    return false;
  }
  if (message?.type === 'shopsource-health') {
    api('/api/capture/health',undefined,'GET').then(() => sendResponse({ok:true,status:200})).catch(() => sendResponse({ok:false,status:0}));
    return true;
  }
});
