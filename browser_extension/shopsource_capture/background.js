const API_DEFAULT = 'http://127.0.0.1:8081';
const openingRuns = new Set();
const pendingTabs = new Map();
const workerTabs = new Map();
const searchWorkerTabs = new Map();
const PENDING_PREFIX = 'shopsource.pending.';
const WORKER_PREFIX = 'shopsource.worker.';
const SEARCH_WORKER_PREFIX = 'shopsource.searchWorker.';
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
async function navigateCampaign(instruction) {
  if (!instruction?.url || !instruction?.campaign_id) return;
  const campaignId = instruction.campaign_id; const target = new URL(instruction.url);
  target.hash = `shopsource_campaign=${encodeURIComponent(campaignId)}`;
  let record = await validSearchWorker(campaignId); let tab = null;
  let prior = record?.tabId;
  if (!prior) {
    const marked = await chrome.tabs.query({url:'https://www.amazon.com/*'});
    const found = marked.find(candidate => (candidate.url || '').includes(`shopsource_campaign=${encodeURIComponent(campaignId)}`));
    if (found?.id) { prior=found.id; record=await saveSearchWorker(campaignId,found.id,found.url || target.href); }
  }
  if (prior) { try { tab = await chrome.tabs.update(prior,{url:target.href,active:false}); await saveSearchWorker(campaignId,tab.id,target.href,record?.createdAt); await sendCampaignEvent(campaignId,'SEARCH_WORKER_REUSED',{url:target.href}); }
    catch (_error) { await clearSearchWorker(campaignId); } }
  if (!tab) { const searchUrl=target.href; tab = await chrome.tabs.create({url:searchUrl,active:false}); await saveSearchWorker(campaignId,tab.id,searchUrl); await sendCampaignEvent(campaignId,'SEARCH_WORKER_CREATED',{url:searchUrl}); }
}
const searchWorkerKey = campaignId => SEARCH_WORKER_PREFIX + campaignId;
const normalizedSearchPage = value => { try { const url=new URL(value); url.hash=''; for(const key of ['ref','qid','sr','sprefix','crid']) url.searchParams.delete(key); url.searchParams.sort(); return url.href; } catch(_error) { return value || ''; } };
async function saveSearchWorker(campaignId,tabId,lastUrl,createdAt=Date.now(),updates={}) {
  const prior=searchWorkerTabs.get(campaignId) || {};
  const record={campaignId,tabId,createdAt:Number(createdAt)||Date.now(),lastUrl:lastUrl || prior.lastUrl || '',
    captureInFlight:Boolean(prior.captureInFlight),lastCaptureAt:prior.lastCaptureAt || null,
    lastCapturedUrl:prior.lastCapturedUrl || '',...updates};
  searchWorkerTabs.set(campaignId,record);
  try { await chrome.storage.session.set({[searchWorkerKey(campaignId)]:record}); } catch(_error) {}
  return record;
}
async function clearSearchWorker(campaignId) {
  searchWorkerTabs.delete(campaignId);
  try { await chrome.storage.session.remove(searchWorkerKey(campaignId)); } catch(_error) {}
}
async function validSearchWorker(campaignId) {
  let record=searchWorkerTabs.get(campaignId);
  if (!record) {
    try { const stored=(await chrome.storage.session.get(searchWorkerKey(campaignId)))[searchWorkerKey(campaignId)]; if(stored?.campaignId===campaignId&&Number.isInteger(stored.tabId)){record=stored;searchWorkerTabs.set(campaignId,record);} } catch(_error) {}
  }
  if (!record) return null;
  try { const tab=await chrome.tabs.get(record.tabId); if(tab.url&&tab.url!==record.lastUrl) record=await saveSearchWorker(campaignId,tab.id,tab.url,record.createdAt); return record; }
  catch(_error) { await clearSearchWorker(campaignId); await sendCampaignEvent(campaignId,'SEARCH_WORKER_MISSING',{error:'Persisted search worker tab is missing'}); return null; }
}
async function sendCampaignEvent(campaignId,event,payload={}) {
  try { return await api(`/api/capture/campaigns/${encodeURIComponent(campaignId)}/events`,{event,payload}); }
  catch(_error) { return null; }
}
async function restoreSearchWorkers() {
  try {
    const values=await chrome.storage.session.get(null);
    for(const [key,stored] of Object.entries(values)) {
      if(!key.startsWith(SEARCH_WORKER_PREFIX)||!stored?.campaignId||!Number.isInteger(stored.tabId)) continue;
      try {
        const tab=await chrome.tabs.get(stored.tabId);
        const record={...stored,lastUrl:tab.url || stored.lastUrl,captureInFlight:false};
        searchWorkerTabs.set(record.campaignId,record);
        await chrome.storage.session.set({[key]:record});
        if(tab.status==='complete') setTimeout(()=>triggerCampaignSearchCapture(tab.id,record.campaignId),0);
      } catch(_error) { await chrome.storage.session.remove(key); await sendCampaignEvent(stored.campaignId,'SEARCH_WORKER_MISSING',{error:'Persisted search worker tab is missing'}); }
    }
  } catch(_error) {}
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const pendingKey = runId => PENDING_PREFIX + runId;
const workerKey = runId => WORKER_PREFIX + runId;

async function saveWorker(runId, tabId, createdAt = Date.now()) {
  const record = {runId,tabId,createdAt};
  workerTabs.set(runId,record);
  await chrome.storage.session.set({[workerKey(runId)]:record});
  return record;
}
async function clearWorker(runId) {
  workerTabs.delete(runId);
  try { await chrome.storage.session.remove(workerKey(runId)); } catch (_error) {}
}
async function validWorker(runId) {
  const record = workerTabs.get(runId);
  if (!record) return null;
  try { await chrome.tabs.get(record.tabId); return record; }
  catch (_error) {
    await clearWorker(runId);
    await extensionEvent(runId,'WORKER_TAB_MISSING','', '', record.tabId);
    return null;
  }
}
async function closeWorker(runId) {
  const record = workerTabs.get(runId);
  if (!record) return false;
  await clearPending(runId);
  await clearWorker(runId);
  try { await chrome.tabs.remove(record.tabId); } catch (_error) {}
  await extensionEvent(runId,'WORKER_TAB_CLOSED','', '', record.tabId);
  return true;
}

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
  // Reused workers keep their tabId, but pending state belongs to the new ASIN.
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
async function restoreWorkerTabs() {
  try {
    const values = await chrome.storage.session.get(null);
    for (const [key,value] of Object.entries(values)) {
      if (!key.startsWith(WORKER_PREFIX) || !value?.runId || !Number.isInteger(value.tabId)) continue;
      try {
        await chrome.tabs.get(value.tabId);
        workerTabs.set(value.runId,{runId:value.runId,tabId:value.tabId,createdAt:Number(value.createdAt)||Date.now()});
      } catch (_error) {
        await chrome.storage.session.remove(key);
        await extensionEvent(value.runId,'WORKER_TAB_MISSING','', '', value.tabId);
      }
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
  const owned = workerTabs.get(runId);
  if (!owned || owned.tabId !== tab.id) return;
  const prior = pendingTabs.get(runId);
  if (prior?.tabId === tab.id && prior.asin === asin) return;
  const record = {tabId:tab.id,asin,createdAt:Date.now(),triggering:false,timer:null};
  await savePending(runId,record);
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
async function triggerCampaignSearchCapture(tabId,campaignId) {
  await restorePromise;
  let campaignState;
  try { campaignState=await api(`/api/capture/campaigns/${encodeURIComponent(campaignId)}`,undefined,'GET'); }
  catch(_error) { return; }
  if(campaignState.status!=='RUNNING') return;
  const record=await validSearchWorker(campaignId);
  if(!record||record.tabId!==tabId) return;
  let tab;
  try { tab=await chrome.tabs.get(tabId); } catch(_error) { return; }
  let parsed;
  try { parsed=new URL(tab.url || ''); } catch(_error) { return; }
  if(!/(^|\.)amazon\.com$/i.test(parsed.hostname)||!/^\/s(?:\/|$)/i.test(parsed.pathname)) return;
  const pageUrl=normalizedSearchPage(tab.url);
  const current=searchWorkerTabs.get(campaignId) || record;
  if(current.captureInFlight||current.lastCapturedUrl===pageUrl) return;
  await saveSearchWorker(campaignId,tabId,tab.url,current.createdAt,{captureInFlight:true});
  await sendCampaignEvent(campaignId,'SEARCH_CAPTURE_TRIGGERED',{keyword:parsed.searchParams.get('k')||'',page:Number(parsed.searchParams.get('page'))||1});
  let failure='SEARCH_CAPTURE_ERROR';
  try {
    for(let attempt=0;attempt<HANDSHAKE_ATTEMPTS;attempt++) {
      const {response,error}=await sendTabMessage(tabId,{type:'shopsource-campaign-capture',campaignId});
      if(response?.ok) {
        const latest=searchWorkerTabs.get(campaignId) || current;
        await saveSearchWorker(campaignId,tabId,tab.url,latest.createdAt,{captureInFlight:false,lastCaptureAt:Date.now(),lastCapturedUrl:pageUrl});
        await sendCampaignEvent(campaignId,'SEARCH_CAPTURE_ACK',{keyword:parsed.searchParams.get('k')||'',page:Number(parsed.searchParams.get('page'))||1});
        const campaign=response.result?.campaign;
        if(campaign?.status==='RUNNING'&&campaign.search_instruction) {
          const instruction=campaign.search_instruction;
          setTimeout(()=>navigateCampaign(instruction),Math.max(6000,Number(instruction.delay_seconds||8)*1000));
        } else if(campaign?.batch_run_id&&campaign.status==='DETAILING') {
          openNext(campaign.batch_run_id);
        }
        return;
      }
      const message=response?.error || '';
      if(message==='CAPTCHA_DETECTED') { failure='SEARCH_CAPTCHA'; break; }
      if(message==='SEARCH_RESULTS_NOT_READY') { failure='SEARCH_RESULTS_NOT_READY'; break; }
      if(!error) { failure='SEARCH_CAPTURE_ERROR'; break; }
      if(attempt<HANDSHAKE_ATTEMPTS-1) await delay(HANDSHAKE_INTERVAL_MS);
    }
  } finally {
    const latest=searchWorkerTabs.get(campaignId) || current;
    await saveSearchWorker(campaignId,tabId,tab.url,latest.createdAt,{captureInFlight:false});
  }
  const event=failure==='SEARCH_CAPTCHA'?'SEARCH_CAPTCHA':failure;
  await sendCampaignEvent(campaignId,event,{keyword:parsed.searchParams.get('k')||'',page:Number(parsed.searchParams.get('page'))||1,
    error:failure==='SEARCH_RESULTS_NOT_READY'?'Amazon search result cards did not become ready within 20 seconds':failure==='SEARCH_CAPTCHA'?'Amazon CAPTCHA/robot check detected':'Search content script handshake receiver did not become ready'});
  if(failure==='SEARCH_CAPTCHA') { try { await chrome.tabs.update(tabId,{active:true}); } catch(_error) {} }
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
        try { await chrome.tabs.update(tabId,{active:true}); } catch (_error) {}
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
      chrome.tabs.update(tabId,{active:true}).catch(()=>{});
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
chrome.tabs.onUpdated.addListener((tabId,changeInfo,tab)=>{
  if(changeInfo.status!=='complete') return;
  restorePromise.then(async()=>{
    const entry=[...searchWorkerTabs.entries()].find(([,record])=>record.tabId===tabId);
    if(!entry) return;
    const [campaignId]=entry;
    const url=changeInfo.url || tab.url || '';
    if(/captcha|robot.?check|validatecaptcha|validate-captcha/i.test(`${url} ${tab.title||''}`)) {
      await sendCampaignEvent(campaignId,'SEARCH_CAPTCHA',{error:'Amazon CAPTCHA/robot check detected'});
      try { await chrome.tabs.update(tabId,{active:true}); } catch(_error) {}
      return;
    }
    triggerCampaignSearchCapture(tabId,campaignId);
  }).catch(()=>{});
});
chrome.tabs.onCreated.addListener(tab => { trackMarkedTab(tab).catch(()=>{}); });
chrome.tabs.onRemoved.addListener(tabId => {
  for(const [campaignId,record] of searchWorkerTabs.entries()) {
    if(record.tabId!==tabId) continue;
    clearSearchWorker(campaignId).then(()=>sendCampaignEvent(campaignId,'SEARCH_WORKER_MISSING',{error:'Search worker tab was closed'})).catch(()=>{});
    break;
  }
  for (const [runId,record] of workerTabs.entries()) {
    if (record.tabId !== tabId) continue;
    clearPending(runId).then(() => clearWorker(runId)).then(() => extensionEvent(runId,'WORKER_TAB_MISSING','', '', tabId)).catch(()=>{});
    break;
  }
});

async function openNext(runId) {
  if (!runId || openingRuns.has(runId)) return;
  openingRuns.add(runId);
  let reserved = null;
  try {
    await restorePromise;
    const result = await api('/api/capture/heartbeat',{event:'NEXT_ITEM',batch_run_id:runId});
    const item = result.item;
    reserved = item;
    if (item?.url && /^https:\/\/(?:www\.)?amazon\.com\//i.test(item.url)) {
      const target = new URL(item.url);
      target.hash = `shopsource_capture=${encodeURIComponent(runId)}`; // fallback/debug marker only
      let worker = await validWorker(runId);
      let tab;
      if (worker) {
        tab = await chrome.tabs.update(worker.tabId,{url:target.href,active:false});
        await extensionEvent(runId,'WORKER_TAB_REUSED',item.asin,'',worker.tabId);
      } else {
        tab = await chrome.tabs.create({url:target.href,active:false});
        worker = await saveWorker(runId,tab.id,Date.now());
        await extensionEvent(runId,'WORKER_TAB_CREATED',item.asin,'',tab.id);
      }
      const record = {tabId:tab.id,asin:item.asin,createdAt:Date.now(),triggering:false,timer:null};
      await savePending(runId,record);
    } else if (!item) {
      try {
        const batch = await api(`/api/capture/batches/${encodeURIComponent(runId)}`,undefined,'GET');
        if (batch.status === 'DONE' || batch.status === 'CANCELLED') await closeWorker(runId);
      } catch (_error) {}
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
      const campaignId=message.kind==='search'?String(message.payload?.campaign_id||''):'';
      if(campaignId) {
        const worker=searchWorkerTabs.get(campaignId);
        if(worker&&sender.tab?.id===worker.tabId) {
          await saveSearchWorker(campaignId,worker.tabId,sender.tab.url||worker.lastUrl,worker.createdAt,
            {captureInFlight:false,lastCaptureAt:Date.now(),lastCapturedUrl:normalizedSearchPage(sender.tab.url||worker.lastUrl)});
        }
      }
      sendResponse({ok:true,result});
      if (message.kind === 'search' && !campaignId && result.campaign?.search_instruction) {
        const instruction=result.campaign.search_instruction;
        setTimeout(()=>navigateCampaign(instruction),Math.max(6000,Number(instruction.delay_seconds||8)*1000));
      }
      if (runId && message.kind === 'search') openNext(runId);
      if (runId && message.kind === 'detail') setTimeout(() => openNext(runId),4000);
    }).catch(error => sendResponse({ok:false,error:error.message}));
    return true;
  }
  if (message?.type === 'shopsource-batch-next') {
    openNext(message.runId).then(() => sendResponse({ok:true})).catch(() => sendResponse({ok:false}));
    return true;
  }
  if (message?.type === 'shopsource-campaign-command') {
    api(`/api/capture/campaigns/${encodeURIComponent(message.campaignId)}/action`,{action:message.action||'RESUME'}).then(async campaign=>{
      if(campaign.search_instruction) {
        const worker=await validSearchWorker(message.campaignId);
        if(worker) {
          const tab=await chrome.tabs.get(worker.tabId).catch(()=>null);
          if(tab&&tab.status==='complete'&&worker.lastCapturedUrl!==normalizedSearchPage(tab.url||'')) triggerCampaignSearchCapture(tab.id,message.campaignId);
          else await navigateCampaign(campaign.search_instruction);
        } else await navigateCampaign(campaign.search_instruction);
      }
      if(campaign.batch_run_id&&campaign.status==='DETAILING') await openNext(campaign.batch_run_id);
      sendResponse({ok:true,campaign});
    }).catch(error=>sendResponse({ok:false,error:error.message})); return true;
  }
  if(message?.type==='shopsource-campaign-event') {
    sendCampaignEvent(String(message.campaignId||''),String(message.event||''),message.payload||{}).then(()=>sendResponse({ok:true}));
    return true;
  }
  if (message?.type === 'shopsource-campaign-captcha') {
    if(message.campaignId) sendCampaignEvent(message.campaignId,'SEARCH_CAPTCHA',{error:'Amazon CAPTCHA/robot check detected'});
    const record=searchWorkerTabs.get(message.campaignId); if(record) chrome.tabs.update(record.tabId,{active:true}).catch(()=>{}); return false;
  }
  if (message?.type === 'shopsource-worker-command') {
    const runId = message.runId;
    Promise.resolve().then(async () => {
      await restorePromise;
      const worker = await validWorker(runId);
      if (message.action === 'status') return {ok:true,exists:Boolean(worker),tabId:worker?.tabId};
      if (message.action === 'show') {
        if (!worker) return {ok:false,error:'WORKER_TAB_MISSING'};
        await chrome.tabs.update(worker.tabId,{active:true});
        return {ok:true,exists:true,tabId:worker.tabId};
      }
      if (message.action === 'close') {
        if (pendingTabs.has(runId) && !message.force) return {ok:false,error:'CAPTURE_IN_PROGRESS'};
        return {ok:true,closed:await closeWorker(runId)};
      }
      return {ok:false,error:'UNSUPPORTED_ACTION'};
    }).then(sendResponse).catch(error => sendResponse({ok:false,error:error.message}));
    return true;
  }
  if (message?.type === 'shopsource-batch-captcha' || message?.type === 'shopsource-batch-failed') {
    const event = message.type === 'shopsource-batch-captcha' ? 'CAPTCHA_DETECTED' : 'DETAIL_CAPTURE_FAILED';
    if (event === 'CAPTCHA_DETECTED' && message.runId) {
      validWorker(message.runId).then(worker => worker && chrome.tabs.update(worker.tabId,{active:true})).catch(()=>{});
    }
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

const restorePromise = Promise.all([restoreWorkerTabs(),restorePendingTabs(),restoreSearchWorkers()]);
