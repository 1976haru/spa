(() => {
  const trustedOrigin = 'http://127.0.0.1:8081';
  if (location.origin !== trustedOrigin) return;
  window.addEventListener('message', event => {
    const message = event.data;
    if (event.source !== window || event.origin !== trustedOrigin || message?.source !== 'shopsource-studio-ui') return;
    if (typeof message.runId !== 'string' || !/^BB_[a-f0-9]{20}$/i.test(message.runId)) return;
    const workerActions = {'worker-show':'show','worker-close':'close','worker-status':'status'};
    const isBatchNext = message.type === 'batch-open-next';
    const action = workerActions[message.type];
    if (!isBatchNext && !action) return;
    const runtimeMessage = isBatchNext
      ? {type:'shopsource-batch-next',runId:message.runId}
      : {type:'shopsource-worker-command',runId:message.runId,action,force:message.force === true};
    chrome.runtime.sendMessage(runtimeMessage,response => {
      const error = chrome.runtime.lastError;
      window.postMessage({source:'shopsource-capture-extension',type:isBatchNext?'batch-open-result':'worker-command-result',requestId:message.requestId || '',action,ok:!error && response?.ok !== false,error:error?.message || response?.error || '',exists:response?.exists,closed:response?.closed},trustedOrigin);
    });
  });
})();
