function connectSSE(url) {
  const logContainer = document.getElementById('log-container');
  const progressBar = document.getElementById('progress-bar');
  const progressPct = document.getElementById('progress-pct');
  const doneBanner = document.getElementById('done-banner');
  const errorBanner = document.getElementById('error-banner');

  if (!logContainer) return;

  let completed = false;
  let hasError = false;

  function showDone() {
    if (completed || hasError) return;
    completed = true;
    if (progressBar) progressBar.style.width = '100%';
    if (progressPct) progressPct.textContent = '100%';
    if (doneBanner) {
      doneBanner.style.display = 'flex';
      const countdown = document.createElement('span');
      countdown.className = 'countdown-msg';
      doneBanner.appendChild(countdown);
      let secs = 3;
      countdown.textContent = ` Returning to dashboard in ${secs}s…`;
      const iv = setInterval(() => {
        secs--;
        if (secs <= 0) {
          clearInterval(iv);
          window.location.href = doneBanner.querySelector('a[href*="index"]')?.href || '/';
        } else {
          countdown.textContent = ` Returning to dashboard in ${secs}s…`;
        }
      }, 1000);
    }
  }

  const es = new EventSource(url);

  es.onmessage = function(e) {
    const data = JSON.parse(e.data);

    if (data.done) {
      es.close();
      showDone();
      return;
    }

    const pct = data.percent_complete || 0;
    if (progressBar) progressBar.style.width = pct + '%';
    if (progressPct) progressPct.textContent = pct + '%';

    const entry = document.createElement('div');
    entry.className = 'log-entry';
    const ts = new Date(data.timestamp).toLocaleTimeString();
    entry.innerHTML = `
      <span class="log-ts">${ts}</span>
      <span class="log-step ${data.status}">[${data.step}]</span>
      <span class="log-msg">${data.message}</span>`;
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;

    if (data.status === 'error') {
      hasError = true;
      if (errorBanner) errorBanner.style.display = 'flex';
      es.close();
    }
  };

  es.onerror = function() {
    es.close();
    if (!hasError) showDone();
  };
}
