const API_DEFAULT = 'http://127.0.0.1:8081';
const openingRuns = new Set();
const pendingTabs = new Map();
const PENDING_PREFIX = 'shopsource.pending.';
const OPEN_TIMEOUT_MS = 45000;
const HANDSHAKE_ATTEMPTS = 10;
const HANDSHAKE_INTERVAL_MS = 500;

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
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const pendingKey = runId => PENDING_PREFIX + runId;

function armTimeout(runId, record, remainingMs = OPEN_TIMEOUT_MS) {
  if (record.timer) clearTimeout(record.timer);
  record.timer = setTimeout(async () => {
    if (pendingTabs.get(runId)?.tabId !== record.tabId) return;
    await clearPending(runId);
    try {
      await api('/api/capture/heartbeat',{event:'DETAIL_CAPTURE_FAILED',batch_run_id:runId,asin:record.asin,reason:'Detail page capture timed out'});
      setTimeout(() => openNext(runId),4000);
    } catch (_error) { /* persisted batch state remains available for operator recovery */ }
  }, Math.max(0, remainingMs));
}
async function savePending(runId, record) {
  const current = pendingTabs.get(runId);
  if (current?.tabId === record.tabId) {
    if (!current.timer) armTimeout(runId,current,Math.max(0,OPEN_TIMEOUT_MS-(Date.now()-current.createdAt)));
    return current;
  }
  if (current?.timer) clearTimeout(current.timer);
  pendingTabs.set(runId, record);
  await chrome.storage.session.set({[pendingKey(runId)]:{runId,tabId:record.tabId,asin:record.asin,createdAt:record.createdAt}});
  armTimeout(runId, record, Math.max(0, OPEN_TIMEOUT_MS - (Date.now() - record.createdAt)));
}
async function clearPending(runId) {
  const record = pendingTabs.get(runId);
  if (record?.timer) clearTimeout(record.timer);
  pendingTabs.delete(runId);
  try { await chrome.storage.session.remove(pendingKey(runId)); } catch (_error) { /* session storage may be unavailable during shutdown */ }
}
async function getPendingByTab(tabId) {
  for (const [runId, record] of pendingTabs.entries()) if (record.tabId === tabId) return {runId, ...record};
  try {
    const values = await chrome.storage.session.get(null);
    for (const [key, value] of Object.entries(values)) {
      if (key.startsWith(PENDING_PREFIX) && value?.tabId === tabId && value?.runId) {
        const record = {tabId:value.tabId,asin:value.asin,createdAt:value.createdAt,triggering:false};
        await savePending(value.runId, record);
        return {runId:value.runId, ...record};
      }
    }
  } catch (_error) { /* in-memory state remains authoritative for this worker lifetime */ }
  return null;
}
async function restorePendingTabs() {
  try {
    const values = await chrome.storage.session.get(null);
    for (const [key, value] of Object.entries(values)) {
      if (!key.startsWith(PENDING_PREFIX) || !value?.runId || !Number.isInteger(value.tabId) || !value.asin) continue;
      try {
        const tab = await chrome.tabs.get(value.tabId);
        const record = {tabId:value.tabId,asin:value.asin,createdAt:Number(value.createdAt)||Date.now(),triggering:false};
        await savePending(value.runId,record);
        if (tab.status === 'complete' && /^https:\/\/(?:www\.)?amazon\.com\/(?:dp|gp\/product)\/[A-Z0-9]{10}(?:\/|$)/i.test(tab.url || '')) {
          triggerAutoCapture(tab.id,value.runId,value.asin,record);
        }
      } catch (_error) { await clearPending(value.runId); }
    }
  } catch (_error) { /* storage.session is scoped to this browser session */ }
}
async function extensionEvent(runId, eventName, asin, reason = '', tabId) {
  try { await api('/api/capture/heartbeat',{event:'EXTENSION_EVENT',batch_run_id:runId,event_name:eventName,asin,reason,tab_id:tabId}); }
  catch (_error) { /* event logging must not break the capture handshake */ }
}
async function trackMarkedTab(tab) {
  if (!tab?.id) return;
  const url = tab.pendingUrl || tab.url || '';
  let parsed;
  try { parsed = new URL(url); } catch (_error) { return; }
  if (!/(^|\.)amazon\.com$/i.test(parsed.hostname)) return;
  const asin = parsed.pathname.match(/^\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:\/|$)/i)?.[1]?.toUpperCase();
  const runId = new URLSearchParams(parsed.hash.slice(1)).get('shopsource_capture');
  if (!asin || !runId) return;
  const prior = pendingTabs.get(runId);
  if (prior?.tabId === tab.id) return;
  if (prior) {
    await clearPending(runId);
    try { await chrome.tabs.remove(prior.tabId); } catch (_error) { /* old/recovered tab may already be closed */ }
  }
  const record = {tabId:tab.id,asin,createdAt:Date.now(),triggering:false,timer:null};
  await savePending(runId,record);
  await extensionEvent(runId,'TAB_CREATED',asin,'',tab.id);
}
async function failDetail(runId, record, reasonCode, message) {
  await extensionEvent(runId,'AUTO_CAPTURE_ERROR',record.asin,reasonCode,record.tabId);
  await clearPending(runId);
  try { await api('/api/capture/heartbeat',{event:'DETAIL_CAPTURE_FAILED',batch_run_id:runId,asin:record.asin,reason:message}); }
  catch (_error) { /* retain local diagnostics and let the batch timeout safety net apply */ }
  setTimeout(() => openNext(runId),4000);
}
function sendTabMessage(tabId, message) {
  return new Promise(resolve => {
    chrome.tabs.sendMessage(tabId,message,response => {
      const error = chrome.runtime.lastError;
      resolve({response,error:error?.message || ''});
    });
  });
}
async function triggerAutoCapture(tabId, runId, asin, record) {
  if (record.triggering) return;
  record.triggering = true;
  await extensionEvent(runId,'AUTO_CAPTURE_TRIGGERED',asin,'',tabId);
  for (let attempt=0; attempt<HANDSHAKE_ATTEMPTS; attempt++) {
    const {response,error} = await sendTabMessage(tabId,{type:'shopsource-batch-auto-capture',runId,expectedAsin:asin});
    if (response?.ok) {
      await clearPending(runId);
      setTimeout(() => openNext(runId),4000);
      return;
    }
    if (!error) {
      const message = response?.error || 'Detail capture failed';
      if (message === 'CAPTCHA_DETECTED') {
        await clearPending(runId);
        try { await api('/api/capture/heartbeat',{event:'CAPTCHA_DETECTED',batch_run_id:runId,asin}); } catch (_error) {}
        return;
      }
      const reasonCode = 'CONTENT_CAPTURE_FAILED';
      await failDetail(runId,record,reasonCode,message);
      return;
    }
    if (attempt < HANDSHAKE_ATTEMPTS - 1) await delay(HANDSHAKE_INTERVAL_MS);
  }
  await failDetail(runId,record,'RECEIVER_NOT_READY','Detail capture handshake receiver did not become ready');
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url && changeInfo.status !== 'complete' && !tab.title) return;
  const changedUrl = changeInfo.url || tab.url || '';
  getPendingByTab(tabId).then(async found => {
    if (!found) await trackMarkedTab({id:tabId,url:changedUrl,pendingUrl:tab.pendingUrl});
    const pending = await getPendingByTab(tabId);
    if (!pending) return;
    const url = changeInfo.url || tab.url || '';
    const title = tab.title || '';
    if (/captcha|robot.?check|validatecaptcha|validate-captcha/i.test(`${url} ${title}`)) {
      clearPending(pending.runId);
      api('/api/capture/heartbeat',{event:'CAPTCHA_DETECTED',batch_run_id:pending.runId,asin:pending.asin}).catch(()=>{});
      return;
    }
    if (changeInfo.status !== 'complete') return;
    let parsed;
    try { parsed = new URL(url); } catch (_error) { return; }
    if (!/(^|\.)amazon\.com$/i.test(parsed.hostname)) return;
    // Intermediate redirect pages are allowed to finish; only a final product path is handshaken.
    if (!/^\/(?:dp|gp\/product)\/[A-Z0-9]{10}(?:\/|$)/i.test(parsed.pathname)) return;
    const record = pendingTabs.get(pending.runId);
    if (record) triggerAutoCapture(tabId,pending.runId,pending.asin,record);
  }).catch(()=>{});
});
chrome.tabs.onCreated.addListener(tab => { trackMarkedTab(tab).catch(()=>{}); });

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
      target.hash = `shopsource_capture=${encodeURIComponent(runId)}`; // fallback/debug marker only
      const tab = await chrome.tabs.create({url:target.href,active:true});
      const alreadyTracked = pendingTabs.get(runId)?.tabId === tab.id;
      const record = {tabId:tab.id,asin:item.asin,createdAt:Date.now(),triggering:false,timer:null};
      await savePending(runId,record);
      if (!alreadyTracked) await extensionEvent(runId,'TAB_CREATED',item.asin,'',tab.id);
    }
  } catch (_error) {
    if (reserved?.asin) {
      try {
        await extensionEvent(runId,'AUTO_CAPTURE_ERROR',reserved.asin,'TAB_CREATE_FAILED');
        await api('/api/capture/heartbeat',{event:'DETAIL_CAPTURE_FAILED',batch_run_id:runId,asin:reserved.asin,reason:'Could not open the detail tab'});
      } catch (_ignored) { /* persisted state remains available for operator recovery */ }
    }
  } finally { openingRuns.delete(runId); }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'shopsource-capture') {
    sendCapture(message.kind,message.payload).then(async result => {
      const runId = result.batch?.run_id || message.payload?.batch_run_id;
      if (message.kind === 'detail' && runId) {
        await extensionEvent(runId,'AUTO_CAPTURE_ACK',message.payload?.product?.asin || '','CAPTURE_ACKNOWLEDGED',sender.tab?.id);
        await clearPending(runId);
      }
      sendResponse({ok:true,result});
      if (runId && message.kind === 'search') openNext(runId);
      if (runId && message.kind === 'detail') setTimeout(() => openNext(runId),4000);
    }).catch(error => sendResponse({ok:false,error:error.message}));
    return true;
  }
  if (message?.type === 'shopsource-batch-next') {
    openNext(message.runId).then(() => sendResponse({ok:true})).catch(() => sendResponse({ok:false}));
    return true;
  }
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

restorePendingTabs();
