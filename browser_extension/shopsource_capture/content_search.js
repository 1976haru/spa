(() => {
  const SELECTOR = '[data-component-type="s-search-result"][data-asin]';
  const inFlight = new Map();
  const completed = new Map();
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(
    `${document.title} ${(document.body?.innerText || '').slice(0, 3000)}`);
  const text = (root, selectors) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const value = (node?.innerText || node?.textContent || node?.getAttribute('aria-label') || node?.getAttribute('title') || '').trim();
      if (value) return value;
    }
    return '';
  };
  const visible = card => {
    if (card.hidden || card.getAttribute('aria-hidden') === 'true' || !card.getClientRects().length) return false;
    const style = getComputedStyle(card);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };
  const validCards = () => [...document.querySelectorAll(SELECTOR)].filter(card =>
    visible(card) && /^[A-Z0-9]{10}$/.test((card.getAttribute('data-asin') || '').trim().toUpperCase()));
  const normalizedUrl = () => {
    const url = new URL(location.href); url.hash = '';
    for (const key of ['ref','qid','sr','sprefix','crid']) url.searchParams.delete(key);
    url.searchParams.sort(); return url.href;
  };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function waitForResults() {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      if (isCaptcha()) return {status:'CAPTCHA'};
      const cards = validCards();
      if (cards.length) return {status:'READY', cards};
      await wait(500);
    }
    return isCaptcha() ? {status:'CAPTCHA'} : {status:'TIMEOUT'};
  }
  const nextLink = () => {
    const node = document.querySelector('a.s-pagination-next:not(.s-pagination-disabled), a[aria-label="Go to next page"]');
    if (!node?.href) return null;
    try {
      const url = new URL(node.href, location.href);
      return /(^|\.)amazon\.com$/i.test(url.hostname) ? url.href : null;
    } catch (_error) { return null; }
  };
  function buildPayload(cards, campaignId) {
    const pageUrl = new URL(location.href);
    const keyword = pageUrl.searchParams.get('k') || '';
    const page = Number(pageUrl.searchParams.get('page')) || 1;
    const unique = new Map();
    for (const card of cards) {
      const asin = (card.getAttribute('data-asin') || '').trim().toUpperCase();
      const title = text(card, ['h2 span','h2 a span','h2 a[aria-label]','[data-cy="title-recipe"] span','[data-cy="title-recipe"] h2','a.a-link-normal.s-line-clamp-2']);
      const priceText = card.querySelector('.a-price .a-offscreen')?.textContent?.trim() || '';
      const price = Number(priceText.replace(/[^0-9.]/g, ''));
      const ratingText = text(card, ['[aria-label*="out of 5 stars"]','[aria-label*="stars"]','.a-icon-alt']);
      const reviewText = text(card, ['[aria-label*="ratings"]','[aria-label*="reviews"]','.a-size-base.s-underline-text']);
      const image = card.querySelector('img.s-image, img');
      unique.set(asin, {asin,title,url:`https://www.amazon.com/dp/${asin}`,
        price:Number.isFinite(price)&&price>0?price:null, images:image?.src?[image.src]:[],
        rating:Number((ratingText.match(/[0-9]+(?:\.[0-9]+)?/)||[])[0])||null,
        reviewCount:Number((reviewText.replace(/,/g,'').match(/[0-9]+/)||[])[0])||null,
        sponsored:/sponsored/i.test(card.innerText.slice(0,500))?true:null,
        _sourceUrl:location.href,_listPage:page,_collectedAt:new Date().toISOString()});
    }
    const next = nextLink();
    return {source_kind:'BROWSER_CAPTURE_SEARCH', marketplace:'US', search_url:location.href,
      keyword, page_number:page, captured_at:new Date().toISOString(), products:[...unique.values()],
      campaign_id:campaignId, next_url:next, exhausted:!next};
  }
  async function reportCampaignEvent(campaignId, event, payload = {}) {
    if (!campaignId) return;
    try { await chrome.runtime.sendMessage({type:'shopsource-campaign-event',campaignId,event,payload}); } catch (_error) {}
  }
  async function submit(campaignId = '') {
    if (isCaptcha()) {
      await reportCampaignEvent(campaignId,'SEARCH_CAPTCHA',{keyword:new URL(location.href).searchParams.get('k')||'',page:Number(new URL(location.href).searchParams.get('page'))||1,error:'Amazon CAPTCHA/robot check detected'});
      return {ok:false,error:'CAPTCHA_DETECTED'};
    }
    if (campaignId) {
      const key = `${campaignId}|${normalizedUrl()}`;
      if (completed.has(key)) return completed.get(key);
      if (inFlight.has(key)) return inFlight.get(key);
      const promise = (async () => {
        const readiness = await waitForResults();
        if (readiness.status === 'CAPTCHA') {
          await reportCampaignEvent(campaignId,'SEARCH_CAPTCHA',{keyword:new URL(location.href).searchParams.get('k')||'',page:Number(new URL(location.href).searchParams.get('page'))||1,error:'Amazon CAPTCHA/robot check detected'});
          return {ok:false,error:'CAPTCHA_DETECTED'};
        }
        if (readiness.status !== 'READY') {
          const error = 'SEARCH_RESULTS_NOT_READY';
          await reportCampaignEvent(campaignId,error,{keyword:new URL(location.href).searchParams.get('k')||'',page:Number(new URL(location.href).searchParams.get('page'))||1,error:'No visible valid Amazon search result cards after 20 seconds'});
          return {ok:false,error};
        }
        let captured;
        try { captured = await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:buildPayload(readiness.cards,campaignId)}); }
        catch (_error) { captured = {ok:false,error:'ShopSource extension bridge disconnected'}; }
        if (!captured?.ok) {
          const error = captured?.error || 'SEARCH_CAPTURE_ERROR';
          await reportCampaignEvent(campaignId,'SEARCH_CAPTURE_ERROR',{keyword:new URL(location.href).searchParams.get('k')||'',page:Number(new URL(location.href).searchParams.get('page'))||1,error});
          return {ok:false,error};
        }
        const result = {ok:true,result:captured.result}; completed.set(key,result); return result;
      })().finally(() => inFlight.delete(key));
      inFlight.set(key,promise);
      return promise;
    }
    const readiness = await waitForResults();
    if (readiness.status !== 'READY') return {ok:false,error:readiness.status==='CAPTCHA'?'CAPTCHA_DETECTED':'SEARCH_RESULTS_NOT_READY'};
    return chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:buildPayload(readiness.cards,'')});
  }
  if (!/\/s(?:\/|\?|$)/.test(location.pathname)) return;
  const button = document.createElement('button'); button.textContent = 'ShopSource 검색 결과 캡처';
  Object.assign(button.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'12px 16px',background:'#2563eb',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
  button.addEventListener('click',async()=>{
    button.disabled=true;
    const result=await submit('');
    if (!result?.ok) { button.disabled=false; alert(result?.error||'ShopSource connection failed.'); }
    else button.textContent=`ShopSource: ${result.result?.candidates||0}개 캡처`;
  });
  document.documentElement.appendChild(button);
  let explicitCampaignRequested = false;
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
    if(message?.type==='shopsource-page-info') sendResponse({kind:'search'});
    if(message?.type==='shopsource-campaign-capture') { explicitCampaignRequested=true; submit(message.campaignId).then(sendResponse); return true; }
    if(message?.type==='shopsource-capture-now') { submit('').then(sendResponse); return true; }
  });
  // Campaign capture is initiated only by the explicit background content-script handshake.
  const recoveryCampaignId=new URLSearchParams(location.hash.slice(1)).get('shopsource_campaign');
  if(recoveryCampaignId&&/^(?:LC|AC)_[a-f0-9]{20}$/i.test(recoveryCampaignId)) setTimeout(()=>{
    if(!explicitCampaignRequested) submit(recoveryCampaignId);
  },15000);
})();
