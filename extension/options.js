'use strict';

const form = document.getElementById('settingsForm');
const endpointInput = document.getElementById('endpoint');
const result = document.getElementById('result');
const testButton = document.getElementById('test');

function showResult(message, kind = '') {
  result.textContent = message;
  result.className = `result ${kind}`.trim();
}

async function connect({ save }) {
  let endpoint;
  try {
    endpoint = normalizeEndpoint(endpointInput.value);
  } catch (error) {
    showResult(error.message, 'error');
    endpointInput.focus();
    return;
  }

  showResult('Requesting access…');
  const granted = await requestEndpointPermission(endpoint);
  if (!granted) {
    showResult('Access was not granted. The endpoint was not contacted.', 'error');
    return;
  }

  if (save) {
    await chrome.storage.sync.set({ endpoint });
    endpointInput.value = endpoint;
  }

  showResult('Testing connection…');
  try {
    const data = await fetchUsage(endpoint);
    showResult(`Connected: ${data.summary.ok}/${data.summary.total} providers healthy.`, 'success');
  } catch (error) {
    showResult(`Access granted, but the connection failed: ${error.message}`, 'error');
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  connect({ save: true });
});

testButton.addEventListener('click', () => connect({ save: false }));

storedEndpoint()
  .then(endpoint => { endpointInput.value = endpoint; })
  .catch(error => showResult(error.message, 'error'));
