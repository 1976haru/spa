(() => {
  const SELECTOR = '[data-component-type="s-search-result"][data-asin]';
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(document.title + ' ' + (document.body?.innerText || '').slice(0, 3000));
  const text = (root, selectors) => { for (const selector of selectors) { const node = root.querySelector(selector); const value = (node?.innerText || node?.textContent || node?.getAttribute('aria-label') || '').trim(); if (value) return value; } return ''; };
  const visible = card => card.getClientRects().length && !card.hidden && getComputedStyle(card).display !== 'none';
  const nextLink = () => {
    const node = document.querySelector('a.s-pagination-next:not(.s-pagination-disabled), a[aria-label="Go to next page"]');
    if (!node?.href) return null;
    try { const url = new URL(node.href, location.href); return /(^|\.)amazon\.com$/i.test(url.hostname) ? url.href : null; }
    catch (_error) { return null; }
  };
  function captureResults(campaignId = '') {
    if (isCaptcha()) throw new Error('CAPTCHA_DETECTED');
    const pageUrl = new URL(location.href); const keyword = pageUrl.searchParams.get('k') || '';
    const page = Number(pageUrl.searchParams.get('page')) || 1; const unique = new Map();
    for (const card of document.querySelectorAll(SELECTOR)) {
      if (!visible(card)) continue;
      const asin = (card.getAttribute('data-asin') || '').trim().toUpperCase();
      if (!/^[A-Z0-9]{10}$/.test(asin)) continue;
      const title = text(card, ['h2 span','h2 a span','h2 a[aria-label]','[data-cy="title-recipe"] span','[data-cy="title-recipe"] h2']);
      const priceText = card.querySelector('.a-price .a-offscreen')?.textContent?.trim() || '';
      const price = Number(priceText.replace(/[^0-9.]/g, '')); const image = card.querySelector('img.s-image, img');
      const ratingText = text(card, ['[aria-label*="out of 5 stars"]','[aria-label*="stars"]','.a-icon-alt']);
      const reviewText = text(card, ['[aria-label*="ratings"]','[aria-label*="reviews"]','.a-size-base.s-underline-text']);
      unique.set(asin, {asin,title,url:`https://www.amazon.com/dp/${asin}`,price:Number.isFinite(price)&&price>0?price:null,
        images:image?.src?[image.src]:[],rating:Number((ratingText.match(/[0-9]+(?:\.[0-9]+)?/)||[])[0])||null,
        reviewCount:Number((reviewText.replace(/,/g,'').match(/[0-9]+/)||[])[0])||null,
        sponsored:/sponsored/i.test(card.innerText.slice(0,500))?true:null,_sourceUrl:location.href,_listPage:page,_collectedAt:new Date().toISOString()});
    }
    const products = [...unique.values()]; if (!products.length) throw new Error('Amazon search results were not found.');
    const next = nextLink();
    return {source_kind:'BROWSER_CAPTURE_SEARCH',marketplace:'US',search_url:location.href,keyword,page_number:page,
      captured_at:new Date().toISOString(),products,campaign_id:campaignId,next_url:next,exhausted:!next};
  }
  if (!/\/s(?:\/|\?|$)/.test(location.pathname)) return;
  const button = document.createElement('button'); button.textContent = 'ShopSource 검색 결과 캡처';
  Object.assign(button.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'12px 16px',background:'#2563eb',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
  async function submit(campaignId='') {
    button.disabled=true;
    try { const result=await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:captureResults(campaignId)}); if(!result?.ok) throw new Error(result?.error||'ShopSource connection failed.'); button.textContent=`ShopSource: ${result.result?.candidates||0}개 캡처`; return result; }
    catch(error) { if(error.message==='CAPTCHA_DETECTED') chrome.runtime.sendMessage({type:'shopsource-campaign-captcha',campaignId}); button.disabled=false; if(!campaignId) alert(error.message); return {ok:false,error:error.message}; }
  }
  button.addEventListener('click',()=>submit('')); document.documentElement.appendChild(button);
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
    if(message?.type==='shopsource-page-info') sendResponse({kind:'search'});
    if(message?.type==='shopsource-campaign-capture'){submit(message.campaignId).then(sendResponse);return true;}
    if(message?.type==='shopsource-capture-now'){submit('').then(sendResponse);return true;}
  });
  const campaignId=new URLSearchParams(location.hash.slice(1)).get('shopsource_campaign');
  if(campaignId&&/^LC_[a-f0-9]{20}$/i.test(campaignId)) setTimeout(()=>submit(campaignId),1200);
})();
