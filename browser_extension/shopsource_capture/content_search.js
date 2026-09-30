(() => {
  const SELECTOR = '[data-component-type="s-search-result"][data-asin]';
  const isCaptcha = () => /captcha|robot check|enter the characters you see/i.test(document.title + ' ' + (document.body?.innerText || '').slice(0, 3000));
  const firstText = (root, selectors) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const value = (node?.innerText || node?.textContent || node?.getAttribute('aria-label') || node?.getAttribute('title') || '').trim();
      if (value) return value;
    }
    return '';
  };
  const visibleCard = card => {
    if (card.hidden || card.getAttribute('aria-hidden') === 'true' || !card.getClientRects().length) return false;
    const style = getComputedStyle(card);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };
  function captureResults() {
    if (isCaptcha()) throw new Error('CAPTCHA_DETECTED');
    const pageUrl = new URL(location.href);
    const keyword = pageUrl.searchParams.get('k') || '';
    const page = Number(pageUrl.searchParams.get('page')) || 1;
    const unique = new Map();
    for (const card of document.querySelectorAll(SELECTOR)) {
      if (!visibleCard(card)) continue;
      const asin = (card.getAttribute('data-asin') || '').trim().toUpperCase();
      if (!/^[A-Z0-9]{10}$/.test(asin)) continue;
      const title = firstText(card, [
        'h2 span', 'h2 a span', '[data-cy="title-recipe"] span',
        '[data-cy="title-recipe"] h2', 'a.a-link-normal.s-line-clamp-2',
        'h2 a[aria-label]', 'h2 a[title]',
      ]);
      // Never use sponsored/search tracking hrefs for detail navigation.
      const url = `https://www.amazon.com/dp/${asin}`;
      const priceText = card.querySelector('.a-price .a-offscreen')?.textContent?.trim() || (() => {
        const whole = card.querySelector('.a-price-whole')?.textContent || '';
        const fraction = card.querySelector('.a-price-fraction')?.textContent || '';
        return whole ? `${whole.replace(/[^0-9]/g, '')}.${fraction.replace(/[^0-9]/g, '') || '00'}` : '';
      })();
      const price = Number(priceText.replace(/[^0-9.]/g, ''));
      const ratingText = firstText(card, ['[aria-label*="out of 5 stars"]', '[aria-label*="stars"]', '.a-icon-alt']);
      const reviewText = firstText(card, ['[aria-label*="ratings"]', '[aria-label*="reviews"]', '.a-size-base.s-underline-text']);
      const image = card.querySelector('img.s-image, img');
      const sponsored = /sponsored/i.test(card.innerText.slice(0, 500)) ? true : null;
      const product = {
        asin, title, url,
        price: Number.isFinite(price) && price > 0 ? price : null,
        images: image?.src ? [image.src] : [],
        rating: Number((ratingText.match(/[0-9]+(?:\.[0-9]+)?/) || [])[0]) || null,
        reviewCount: Number((reviewText.replace(/,/g, '').match(/[0-9]+/) || [])[0]) || null,
        sponsored, _sourceUrl: location.href, _listPage: page, _collectedAt: new Date().toISOString(),
      };
      const previous = unique.get(asin);
      if (!previous || (!previous.title && title)) unique.set(asin, product);
    }
    const products = [...unique.values()];
    if (!products.length) throw new Error('Amazon search results were not found.');
    return {source_kind: 'BROWSER_CAPTURE_SEARCH', marketplace: 'US', search_url: location.href, keyword, page_number: page, captured_at: new Date().toISOString(), products};
  }
  if (/\/s(?:\/|\?|$)/.test(location.pathname)) {
    const button = document.createElement('button');
    button.textContent = '현재 검색결과 전체 가져오기';
    Object.assign(button.style, {position:'fixed',right:'16px',bottom:'16px',zIndex:2147483647,padding:'12px 16px',background:'#2563eb',color:'white',border:0,borderRadius:'8px',cursor:'pointer'});
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:captureResults()});
        if (!result?.ok) throw new Error(result?.error || 'ShopSource connection failed.');
        const count = result.result?.candidates || 0;
        const batch = result.result?.batch;
        button.textContent = batch ? `ShopSource: ${batch.last_capture_added}개 추가 · 중복 ${batch.last_capture_duplicates}개` : `ShopSource에 후보 ${count}개 추가됨`;
      } catch (error) {
        if (error.message === 'CAPTCHA_DETECTED') chrome.runtime.sendMessage({type:'shopsource-batch-captcha',keyword:new URL(location.href).searchParams.get('k') || ''});
        button.disabled = false;
        alert(error.message === 'CAPTCHA_DETECTED' ? 'Amazon 확인 화면이 감지되었습니다. 브라우저에서 직접 확인한 뒤 다시 시도하세요.' : error.message);
      }
    });
    document.documentElement.appendChild(button);
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message?.type === 'shopsource-page-info') sendResponse({kind:'search'});
      if (message?.type === 'shopsource-capture-now') {
        try { chrome.runtime.sendMessage({type:'shopsource-capture',kind:'search',payload:captureResults()}).then(sendResponse); }
        catch (error) { sendResponse({ok:false,error:error.message}); }
        return true;
      }
    });
  }
})();
