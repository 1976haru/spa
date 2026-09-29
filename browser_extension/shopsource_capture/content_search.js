(() => {
  const SELECTOR = '[data-component-type="s-search-result"][data-asin]';
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(document.title + ' ' + document.body.innerText.slice(0, 3000));
  function captureVisibleResults() {
    if (isCaptcha()) throw new Error('Amazon에서 확인 화면이 감지되었습니다. 브라우저에서 직접 확인한 뒤 다시 시도하세요.');
    const keyword = new URL(location.href).searchParams.get('k') || '';
    const page = Number(new URL(location.href).searchParams.get('page')) || 1;
    const products = [...document.querySelectorAll(SELECTOR)].filter(card => {
      const rect = card.getBoundingClientRect();
      return card.getClientRects().length && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
    })
      .map(card => {
        const asin = (card.getAttribute('data-asin') || '').trim().toUpperCase();
        const title = card.querySelector('h2 span, h2 a span, [data-cy="title-recipe"] span')?.innerText?.trim() || '';
        const anchor = card.querySelector('h2 a[href], a.a-link-normal[href*="/dp/"]');
        const priceText = card.querySelector('.a-price .a-offscreen')?.innerText || '';
        const parsedPrice = Number(priceText.replace(/[^0-9.]/g, ''));
        const ratingText = card.querySelector('[aria-label*="out of 5 stars"]')?.getAttribute('aria-label') || '';
        const reviewText = card.querySelector('[aria-label*="ratings"]')?.getAttribute('aria-label') || '';
        const image = card.querySelector('img.s-image');
        const sponsored = /sponsored/i.test(card.innerText.slice(0, 500)) ? true : null;
        return {asin, title, url: anchor ? new URL(anchor.getAttribute('href'), location.origin).href : null,
          price: Number.isFinite(parsedPrice) && parsedPrice > 0 ? parsedPrice : null,
          images: image?.src ? [image.src] : [], rating: Number((ratingText.match(/[0-9.]+/) || [])[0]) || null,
          reviewCount: Number((reviewText.match(/[0-9,]+/) || [])[0]?.replace(/,/g, '')) || null,
          sponsored, _sourceUrl: location.href, _listPage: page, _collectedAt: new Date().toISOString()};
      }).filter(item => /^[A-Z0-9]{10}$/.test(item.asin) && item.title);
    if (!products.length) throw new Error('Amazon 검색결과를 찾지 못했습니다.');
    return {source_kind: 'BROWSER_CAPTURE_SEARCH', marketplace: 'US', search_url: location.href,
      keyword, page_number: page, captured_at: new Date().toISOString(), products};
  }
  if (/\/s(?:\/|\?|$)/.test(location.pathname)) {
    const button = document.createElement('button'); button.textContent = '현재 검색결과 ShopSource로 가져오기';
    Object.assign(button.style, {position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'12px 16px',background:'#2563eb',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { const r = await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:captureVisibleResults()});
        alert(r?.ok ? `후보 ${r.result.candidates}개를 ShopSource에 저장했습니다.` : (r?.error || 'ShopSource 연결 실패')); }
      catch (e) { alert(e.message); } finally { button.disabled = false; }
    }); document.documentElement.appendChild(button);
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message?.type === 'shopsource-page-info') sendResponse({kind:'search'});
      if (message?.type === 'shopsource-capture-now') {
        try { chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:captureVisibleResults()}).then(sendResponse); }
        catch (e) { sendResponse({ok:false,error:e.message}); } return true;
      }
    });
  }
})();
