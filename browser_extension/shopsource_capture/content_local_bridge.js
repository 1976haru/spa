(() => {
  const trustedOrigin = 'http://127.0.0.1:8081';
  if (location.origin !== trustedOrigin) return;
  window.addEventListener('message', event => {
    const message = event.data;
    if (event.source !== window || event.origin !== trustedOrigin || message?.source !== 'shopsource-studio-ui') return;
    if (message.type !== 'batch-open-next' || typeof message.runId !== 'string' || !/^BB_[a-f0-9]{20}$/i.test(message.runId)) return;
    chrome.runtime.sendMessage({type:'shopsource-batch-next',runId:message.runId},response => {
      const error = chrome.runtime.lastError;
      window.postMessage({source:'shopsource-capture-extension',type:'batch-open-result',requestId:message.requestId || '',ok:!error && response?.ok !== false},trustedOrigin);
    });
  });
})();
