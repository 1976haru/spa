const status=document.getElementById('status');const page=document.getElementById('page');
chrome.runtime.sendMessage({type:'shopsource-health'},r=>{status.textContent=r?.ok?'연결: 정상':'연결 안 됨';});
chrome.tabs.query({active:true,currentWindow:true},tabs=>{const tab=tabs[0];if(!tab?.url?.startsWith('https://www.amazon.com/')){page.textContent='지원하지 않는 페이지';document.getElementById('capture').disabled=true;return;}chrome.tabs.sendMessage(tab.id,{type:'shopsource-page-info'},r=>{page.textContent=r?.kind==='search'?'현재 페이지: 검색':r?.kind==='product'?'현재 페이지: 상품':'지원하지 않는 페이지';});});
document.getElementById('capture').onclick=()=>chrome.tabs.query({active:true,currentWindow:true},tabs=>{const tab=tabs[0];chrome.tabs.sendMessage(tab.id,{type:'shopsource-capture-now'},r=>{status.textContent=r?.ok?'캡처 완료':(r?.error||'연결 안 됨');});});
document.getElementById('options').onclick=()=>chrome.runtime.openOptionsPage();
