(() => {
  const isProduct = /\/(?:dp|gp\/product)\/[A-Z0-9]{10}/i.test(location.pathname);
  const marker = new URLSearchParams(location.hash.slice(1)).get('shopsource_capture');
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(document.title + ' ' + (document.body?.innerText || '').slice(0, 3000));
  if (marker && isCaptcha()) {
    chrome.runtime.sendMessage({type:'shopsource-batch-captcha',runId:marker,asin:location.pathname.match(/\/([A-Z0-9]{10})/i)?.[1] || ''});
    return;
  }
  if (!isProduct) return;
  let autoCapturePromise = null;
  let autoCaptureContext = {};
  let handshakeRequested = false;
  const text = selector => document.querySelector(selector)?.textContent?.trim() || null;
  const first = selectors => { for (const s of selectors) { const value = text(s); if (value) return value; } return null; };
  function captureDetail() {
    if (isCaptcha()) throw new Error('CAPTCHA_DETECTED');
    const asin = location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1]?.toUpperCase()
      || document.querySelector('#ASIN')?.value || document.querySelector('[data-asin]')?.getAttribute('data-asin');
    let product = null;
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(script.textContent);
        const nodes = Array.isArray(parsed) ? parsed : [parsed];
        const flattened = nodes.flatMap(x => Array.isArray(x?.['@graph']) ? [x, ...x['@graph']] : [x]);
        product = flattened.find(x => x && String(x['@type']).includes('Product')) || product;
      } catch (_error) { /* malformed structured data: use DOM fallback */ }
    }
    const offer = product?.offers && (Array.isArray(product.offers) ? product.offers[0] : product.offers);
    const rawImages = product?.image ? (Array.isArray(product.image) ? product.image : [product.image]) : [];
    const domImage = document.querySelector('#landingImage');
    if (domImage?.dataset?.oldHires) rawImages.unshift(domImage.dataset.oldHires);
    try {
      const dynamicImages = JSON.parse(domImage?.getAttribute('data-a-dynamic-image') || '{}');
      const highest = Object.entries(dynamicImages).sort((a,b) => (b[1]?.[0] || 0) * (b[1]?.[1] || 0) - (a[1]?.[0] || 0) * (a[1]?.[1] || 0))[0]?.[0];
      if (highest) rawImages.unshift(highest);
    } catch (_error) { /* keep other observed image URLs */ }
    const priceText = first(['#corePrice_feature_div .a-offscreen', '.a-price .a-offscreen']) || '';
    const price = Number(offer?.price ?? priceText.replace(/[^0-9.]/g, ''));
    const brand = product?.brand?.name || product?.brand || first(['#bylineInfo', '#brand']);
    const bullets = [...document.querySelectorAll('#feature-bullets li span.a-list-item')].map(x => x.textContent.trim()).filter(Boolean);
    const breadcrumbs = [...document.querySelectorAll('#wayfinding-breadcrumbs_feature_div a')].map(x => x.textContent.trim()).filter(Boolean);
    const ratingText = first(['#acrPopover .a-icon-alt', '#acrPopover', '.a-icon-alt']);
    const reviewText = text('#acrCustomerReviewText');
    const options = {};
    document.querySelectorAll('#twister select').forEach(select => { options[select.getAttribute('name') || select.id || 'option'] = [...select.options].filter(x => x.value).map(x => x.text.trim()).filter(Boolean); });
    if (document.querySelector('#twister')) {
      for (const node of document.querySelectorAll('#twister [title], #twister [aria-label]')) {
        const label = (node.getAttribute('title') || node.getAttribute('aria-label') || '').trim();
        if (label) (options.visible || (options.visible = [])).push(label);
      }
    }
    return {asin, url:location.href.split('#')[0], title:product?.name || first(['#productTitle']), brand,
      price:Number.isFinite(price) && price > 0 ? price : null, category:breadcrumbs.join(' > ') || null,
      overview:bullets, aboutThis:[], images:[...new Set(rawImages.filter(x => typeof x === 'string'))],
      rating:Number((String(ratingText || '').match(/[0-9]+(?:\.[0-9]+)?/) || [])[0]) || null,
      reviewCount:Number((String(reviewText || '').replace(/,/g, '').match(/[0-9]+/) || [])[0]) || null,
      options, quantity:null, tags:[], _sourceUrl:location.href.split('#')[0], _listPage:null, _collectedAt:new Date().toISOString()};
  }
  function productDataReady() {
    if ((document.querySelector('#productTitle')?.textContent || '').trim()) return true;
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const value = JSON.parse(script.textContent);
        const nodes = Array.isArray(value) ? value : [value];
        if (nodes.some(node => {
          const candidates = [...(Array.isArray(node?.['@graph']) ? node['@graph'] : []), node];
          return candidates.some(item => item && String(item['@type']).includes('Product') && String(item.name || '').trim());
        })) return true;
      } catch (_error) { /* malformed or still-loading JSON-LD; keep waiting */ }
    }
    return false;
  }
  async function waitForProductReadiness(timeoutMs = 15000, intervalMs = 500) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (isCaptcha()) throw new Error('CAPTCHA_DETECTED');
      if (productDataReady()) return;
      await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
    if (isCaptcha()) throw new Error('CAPTCHA_DETECTED');
    throw new Error('Product detail DOM did not become ready');
  }
  async function submit({auto = false, batchRunId = null, expectedAsin = null} = {}) {
    if (isCaptcha()) {
      const runId = batchRunId || marker;
      if (runId) chrome.runtime.sendMessage({type:'shopsource-batch-captcha',runId,asin:location.pathname.match(/\/([A-Z0-9]{10})/i)?.[1] || ''});
      throw new Error('CAPTCHA_DETECTED');
    }
    const product = captureDetail();
    if (expectedAsin && String(product.asin || '').toUpperCase() !== String(expectedAsin).toUpperCase()) {
      throw new Error('Opened product ASIN did not match queued ASIN');
    }
    const response = await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'detail',payload:{product,batch_run_id:batchRunId || marker || undefined}});
    if (!response?.ok) throw new Error(response?.error || 'ShopSource connection failed.');
    const badge = document.createElement('div');
    badge.textContent = `ShopSource: 상세 저장 완료 (${response.result.completeness_score}%)`;
    Object.assign(badge.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'10px 14px',background:'#0f766e',color:'white',borderRadius:'8px',font:'14px sans-serif'});
    document.documentElement.appendChild(badge);
    if (batchRunId || marker) {
      history.replaceState(null, '', location.pathname + location.search);
    }
    void auto;
    return response.result;
  }
  function runAutoCapture(context = {}) {
    autoCaptureContext = {...autoCaptureContext,...context};
    if (autoCapturePromise) return autoCapturePromise;
    autoCapturePromise = (async () => {
      await waitForProductReadiness(15000, 500);
      return submit({auto:true,...autoCaptureContext});
    })();
    return autoCapturePromise;
  }
  const button = document.createElement('button'); button.textContent='이 상품 ShopSource로 가져오기';
  Object.assign(button.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:2147483646,padding:'12px 16px',background:'#0f766e',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
  button.addEventListener('click',async()=>{button.disabled=true;try{const r=await submit();alert(`상세 저장 완료 · 준비도 ${r.completeness_score}%`);}catch(e){alert(e.message==='CAPTCHA_DETECTED'?'Amazon 확인 화면이 감지되었습니다. 직접 확인한 뒤 다시 시도하세요.':e.message);}finally{button.disabled=false;}});
  document.documentElement.appendChild(button);
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
    if(message?.type==='shopsource-page-info') sendResponse({kind:'product'});
    if(message?.type==='shopsource-capture-now'){submit().then(result=>sendResponse({ok:true,result})).catch(error=>sendResponse({ok:false,error:error.message}));return true;}
    if(message?.type==='shopsource-batch-auto-capture'){
      if (!isProduct) { sendResponse({ok:false,error:'NON_PRODUCT_PAGE'}); return false; }
      handshakeRequested = true;
      runAutoCapture({batchRunId:message.runId,expectedAsin:message.expectedAsin}).then(result=>sendResponse({ok:true,result})).catch(error=>sendResponse({ok:false,error:error.message}));
      return true;
    }
  });
  if (marker) {
    setTimeout(() => runAutoCapture({batchRunId:marker}).catch(error => {
      const asin = location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1] || '';
      if (error.message === 'CAPTCHA_DETECTED') chrome.runtime.sendMessage({type:'shopsource-batch-captcha',runId:marker,asin});
      else if (!handshakeRequested) chrome.runtime.sendMessage({type:'shopsource-batch-failed',runId:marker,asin,reason:error.message});
    }), 0);
  }
})();
