/* Shared workspace navigation + local launcher bridge. */
/* Load the shared Aerith product design system on every legacy route while pages migrate. */
function installAerithSystem() {
    const styles = [
        ['aerith-tokens', '/system/aerith-tokens.css'],
        ['aerith-base', '/system/aerith-base.css'],
        ['aerith-shell', '/system/aerith-shell.css'],
        ['aerith-components', '/system/aerith-components.css'],
        ['aerith-motion', '/system/aerith-motion.css'],
    ];
    for (const [id, href] of styles) {
        if (document.getElementById(id)) continue;
        const link = document.createElement('link');
        link.id = id;
        link.rel = 'stylesheet';
        link.href = href;
        document.head.append(link);
    }

    const path = location.pathname;
    const page =
        path.startsWith('/ingestion') || path.endsWith('/ingestion.html') ? 'sources' :
        path.startsWith('/clips') || path.endsWith('/clips.html') ? 'clips' :
        path.startsWith('/channels') || path.endsWith('/channels.html') ? 'channel' :
        path.startsWith('/ai') || path.endsWith('/ai.html') ? 'ai' :
        path.startsWith('/editing') || path.endsWith('/editing.html') ? 'production' :
        path.startsWith('/studio') || path.endsWith('/studio.html') ? 'studio' :
        'trends';
    document.body.dataset.katchaPage = page;

    const theme = document.querySelector('meta[name="theme-color"]');
    if (theme) theme.content = '#090b12';
}

function currentWorkspace() {
    const path = location.pathname;
    if (path.startsWith('/ingestion') || path.endsWith('/ingestion.html')) {
        return ['Content sources', '/ingestion'];
    }
    if (path.startsWith('/clips') || path.endsWith('/clips.html')) {
        return ['Clip library', '/clips'];
    }
    if (path.startsWith('/channels') || path.endsWith('/channels.html')) {
        return ['Channel Studio', '/channels'];
    }
    if (path.startsWith('/ai') || path.endsWith('/ai.html')) {
        return ['Katcha AI', '/ai'];
    }
    if (path.startsWith('/editing') || path.endsWith('/editing.html')) {
        return ['Editing control center', '/editing'];
    }
    if (path.startsWith('/studio') || path.endsWith('/studio.html')) {
        return ['Clip Studio', '/studio'];
    }
    return ['Trend explorer', '/explorer'];
}

installAerithSystem();

function installWorkspaceMenu() {
    const brand = document.querySelector('aside .brand, aside .logo');
    const aside = brand?.closest('aside');
    if (!brand || !aside || aside.querySelector('.workspace-menu')) return;

    const [currentLabel, currentHref] = currentWorkspace();
    const cluster = document.createElement('div');
    cluster.className = 'workspace-nav-cluster';
    brand.before(cluster);
    cluster.append(brand);

    const details = document.createElement('details');
    details.className = 'workspace-menu';
    details.innerHTML = `
        <summary aria-label="Open workspace menu">
            <span class="workspace-menu-copy">
                <small>WORKSPACE</small>
                <strong>${currentLabel}</strong>
            </span>
            <span class="workspace-menu-chevron" aria-hidden="true">⌄</span>
        </summary>
        <div class="workspace-menu-popover">
            <a href="/explorer" ${currentHref === '/explorer' ? 'aria-current="page"' : ''}><span>◉</span><b>Trend explorer</b></a>
            <a href="/ai" ${currentHref === '/ai' ? 'aria-current="page"' : ''}><span>✦</span><b>Katcha AI</b></a>
            <a href="/ingestion" ${currentHref === '/ingestion' ? 'aria-current="page"' : ''}><span>↳</span><b>Content sources</b></a>
            <a href="/clips" ${currentHref === '/clips' ? 'aria-current="page"' : ''}><span>▤</span><b>Clip library</b></a>
            <a href="/channels" ${currentHref === '/channels' ? 'aria-current="page"' : ''}><span>▦</span><b>Channel Studio</b></a>
            <a href="/editing" ${currentHref === '/editing' ? 'aria-current="page"' : ''}><span>◇</span><b>Editing control center</b></a>
            <a href="/studio" ${currentHref === '/studio' ? 'aria-current="page"' : ''}><span>⌁</span><b>Clip Studio</b></a>
            ${location.port === '8765' ? '<a href="/"><span>⌂</span><b>Launch console & diagnostics</b></a>' : ''}
        </div>
    `;
    cluster.append(details);

    const legacyNav = aside.querySelector('.rail-nav, nav[aria-label="Workspace"], nav:not(.stats)');
    if (legacyNav) legacyNav.remove();
    const legacyLabel = aside.querySelector('.rail-label, .sidebar-label');
    if (legacyLabel) legacyLabel.remove();

    if (!document.getElementById('workspace-nav-styles')) {
        const style = document.createElement('style');
        style.id = 'workspace-nav-styles';
        style.textContent = `
            .workspace-nav-cluster{display:grid;gap:18px;width:100%;position:relative;z-index:30}
            .workspace-menu{position:relative;margin:0!important;padding:0!important;border:0!important;font-size:inherit!important}
            .workspace-menu>summary{list-style:none;display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;padding:11px 12px;border:1px solid var(--line,#343049);border-radius:11px;background:rgba(255,255,255,.035);cursor:pointer;color:var(--text,#f1eefb)}
            .workspace-menu>summary::-webkit-details-marker{display:none}
            .workspace-menu-copy{display:grid;gap:2px;min-width:0;text-align:left}
            .workspace-menu-copy small{color:var(--muted,#8796a6);font-size:8px;font-weight:850;letter-spacing:.15em}
            .workspace-menu-copy strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:750}
            .workspace-menu-chevron{color:var(--muted,#8796a6);font-size:15px;transition:transform .16s ease}
            .workspace-menu[open] .workspace-menu-chevron{transform:rotate(180deg)}
            .workspace-menu-popover{position:absolute;top:calc(100% + 7px);left:0;right:0;display:grid;gap:4px;padding:7px;border:1px solid var(--line,#343049);border-radius:12px;background:var(--rail,var(--panel,#131120));box-shadow:0 18px 48px #0008;min-width:210px}
            .workspace-menu-popover a{display:grid;grid-template-columns:22px minmax(0,1fr);align-items:center;gap:9px;padding:10px;border-radius:8px;color:var(--muted,#8796a6);text-decoration:none;font-size:12px}
            .workspace-menu-popover a:hover,.workspace-menu-popover a[aria-current="page"]{background:rgba(170,139,250,.14);color:var(--text,#f1eefb)}
            .workspace-menu-popover a[aria-current="page"]{box-shadow:inset 3px 0 var(--purple,var(--lime,#c8f36a))}
            .workspace-menu-popover span{text-align:center}
            .workspace-menu-popover b{font-weight:700;min-width:0}
            @media(max-width:950px){
                .rail .workspace-nav-cluster{width:min(255px,100%);gap:9px}
            }
            @media(max-width:700px){
                .sidebar .workspace-nav-cluster{width:min(280px,100%);gap:9px}
            }
        `;
        document.head.append(style);
    }
}

installWorkspaceMenu();

function installChatShortcut() {
    if (document.getElementById('katcha-chat-shortcut')) return;
    const isChat = currentWorkspace()[1] === '/ai';
    const shortcut = document.createElement('a');
    shortcut.id = 'katcha-chat-shortcut';
    shortcut.href = isChat ? '#prompt' : '/ai?focus=chat';
    shortcut.setAttribute('aria-label', isChat ? 'Jump to Katcha chat' : 'Chat with Katcha');
    shortcut.setAttribute('title', isChat ? 'Jump to chat' : 'Chat with Katcha');
    shortcut.innerHTML = '<span aria-hidden="true">✦</span><span class="katcha-chat-label">Ask Katcha</span>';
    if (isChat) shortcut.addEventListener('click', event => {
        const prompt = document.getElementById('prompt');
        if (!prompt || prompt.closest('[hidden]')) return;
        event.preventDefault();
        prompt.focus();
    });
    const style = document.createElement('style');
    style.textContent = `
        #katcha-chat-shortcut{position:fixed;right:24px;bottom:24px;z-index:1000;
            display:flex;align-items:center;gap:9px;border:1px solid rgba(198,165,255,.55);
            border-radius:999px;padding:12px 17px;background:#2d2147;color:#fff;
            box-shadow:0 12px 38px #0009;text-decoration:none;font:700 13px/1.2 system-ui,sans-serif}
        #katcha-chat-shortcut:hover{background:#443067}
        #katcha-chat-shortcut:focus-visible{outline:3px solid #aee7fa;outline-offset:3px}
        #katcha-chat-shortcut>span:first-child{font-size:18px;color:#c9b4ff}
        @media(max-width:700px){#katcha-chat-shortcut{right:14px;bottom:14px;padding:11px 14px}
            #katcha-chat-shortcut .katcha-chat-label{font-size:12px}}
    `;
    document.head.append(style);
    document.body.append(shortcut);
}
installChatShortcut();

/* The local gateway owns authentication; never expose its token to browser storage. */
(async () => {
    if (location.port !== '8765') return;
    const form = document.getElementById('connect-form') || document.getElementById('connect');
    const connection = document.getElementById('connection');
    const status = document.getElementById('message');
    for (;;) {
        try {
            const response = await fetch('/runtime/status', {cache: 'no-store'});
            if (!response.ok) throw new Error('launcher unavailable');
            const runtime = await response.json();
            if (!runtime.session) return;
            if (runtime.workspace_ready) {
                if (form) {
                    form.hidden = true;
                    form.style.display = 'none';
                    form.requestSubmit();
                }
                return;
            }
            if (connection) {
                connection.textContent = 'WARMING';
                connection.classList.remove('online');
            }
            if (status) {
                status.textContent = runtime.desired_running
                    ? 'Katcha is open. Core services are warming in the background…'
                    : 'Katcha is open. Start services from the launch console when ready.';
            }
        } catch {
            if (connection) connection.textContent = 'RECONNECTING';
            if (status) status.textContent = 'Reconnecting to the local Katcha supervisor…';
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
})();
if (location.port === '8765') {
    const report = (message) => fetch('/runtime/client-error', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: String(message), path: location.pathname}),
    }).catch(() => {});
    window.addEventListener('error', event => report(event.message));
    window.addEventListener('unhandledrejection', event => report(event.reason?.stack || event.reason));
}
