(() => {
  const isProduct = /\/dp\/[A-Z0-9]{10}|\/gp\/product\/[A-Z0-9]{10}/i.test(location.pathname);
  if (!isProduct) return;
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(document.title + ' ' + document.body.innerText.slice(0, 3000));
  const text = selector => document.querySelector(selector)?.textContent?.trim() || null;
  function captureDetail() {
    if (isCaptcha()) throw new Error('Amazon에서 확인 화면이 감지되었습니다. 브라우저에서 직접 확인한 뒤 다시 시도하세요.');
    const asin = location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1]?.toUpperCase()
      || document.querySelector('#ASIN')?.value || document.querySelector('[data-asin]')?.getAttribute('data-asin');
    const ldScripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
    let product = null;
    for (const script of ldScripts) { try { const val=JSON.parse(script.textContent); const arr=Array.isArray(val)?val:[val]; product=arr.find(x=>x && String(x['@type']).includes('Product')) || product; } catch (_e) {} }
    const offer = product?.offers && (Array.isArray(product.offers) ? product.offers[0] : product.offers);
    const imgs = product?.image ? (Array.isArray(product.image) ? product.image : [product.image]) : [];
    const domImage = document.querySelector('#landingImage');
    if (domImage?.dataset?.oldHires) imgs.unshift(domImage.dataset.oldHires);
    const priceText = document.querySelector('#corePrice_feature_div .a-offscreen, .a-price .a-offscreen')?.textContent || '';
    const price = Number(offer?.price ?? priceText.replace(/[^0-9.]/g,''));
    const brand = product?.brand?.name || product?.brand || text('#bylineInfo');
    const bullets = [...document.querySelectorAll('#feature-bullets li span.a-list-item')].map(x=>x.textContent.trim()).filter(Boolean);
    const breadcrumbs = [...document.querySelectorAll('#wayfinding-breadcrumbs_feature_div a')].map(x=>x.textContent.trim()).filter(Boolean);
    const ratingText = text('#acrPopover') || document.querySelector('.a-icon-alt')?.textContent;
    const reviewsText = text('#acrCustomerReviewText');
    const options = {};
    document.querySelectorAll('#twister select').forEach(select => { options[select.getAttribute('name') || select.id || 'option'] = [...select.options].filter(x=>x.value).map(x=>x.text.trim()).filter(Boolean); });
    return {asin, url:location.href, title:product?.name || text('#productTitle'), brand,
      price:Number.isFinite(price) && price>0 ? price : null, category:breadcrumbs.join(' > ') || null,
      overview:bullets, aboutThis:[], images:[...new Set(imgs.filter(Boolean))],
      rating:Number((String(ratingText||'').match(/[0-9.]+/)||[])[0])||null,
      reviewCount:Number((String(reviewsText||'').replace(/,/g,'').match(/[0-9]+/)||[])[0])||null,
      options, quantity:null, tags:[], _sourceUrl:location.href, _listPage:null, _collectedAt:new Date().toISOString()};
  }
  const button=document.createElement('button'); button.textContent='이 상품 ShopSource로 가져오기';
  Object.assign(button.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'12px 16px',background:'#0f766e',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
  button.addEventListener('click',async()=>{button.disabled=true;try{const r=await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'detail',payload:{product:captureDetail()}});alert(r?.ok?`상세 저장 완료 · 준비도 ${r.result.completeness_score}%`:r?.error||'ShopSource 연결 실패');}catch(e){alert(e.message);}finally{button.disabled=false;}});
  document.documentElement.appendChild(button);
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{if(message?.type==='shopsource-page-info')sendResponse({kind:'product'});if(message?.type==='shopsource-capture-now'){try{chrome.runtime.sendMessage({type:'shopsource-capture',kind:'detail',payload:{product:captureDetail()}}).then(sendResponse);}catch(e){sendResponse({ok:false,error:e.message});}return true;}});
})();
