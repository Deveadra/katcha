/* The local gateway owns authentication; never expose its token to browser storage. */
(async () => {
    if (location.port !== '8765') return;
    try {
        const response = await fetch('/runtime/status');
        if (!response.ok) return;
        const runtime = await response.json();
        if (!runtime.session) return;
        const nav = document.querySelector('nav');
        if (nav) {
            const link = document.createElement('a');
            link.href = '/'; link.textContent = 'Launch console & diagnostics'; nav.append(link);
        }
        const form = document.getElementById('connect-form') || document.getElementById('connect');
        if (form) {
            form.hidden = true;
            form.style.display = 'none';
            form.requestSubmit();
        }
    } catch { /* Standard hosted installations retain their existing login flow. */ }
})();
if (location.port === '8765') {
    const report = (message) => fetch('/runtime/client-error', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: String(message), path: location.pathname}),
    }).catch(() => {});
    window.addEventListener('error', event => report(event.message));
    window.addEventListener('unhandledrejection', event => report(event.reason?.stack || event.reason));
}
