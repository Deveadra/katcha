/* Read-only historical inspection. Current editing and review state stays independent. */
window.KatchaEditorialHistory = (() => {
    const el = id => document.getElementById(`editorial-history${id ? `-${id}` : ""}`);
    const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
    const labels = {narration: "Narration generation", analysis: "Source analysis", script: "Research and script", assets: "Asset search", acquire_assets: "Asset download", render: "Video preview"};
    const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Date unavailable";
    const readable = value => String(value || "Unknown").replaceAll("_", " ");
    let api, apiBlob, channel = "", project = "", generation = 0, request = 0;
    let pages = [null], rows = [], more = false, loaded = false, busy = false, selected = null, previewUrl = null;
    const base = () => `/v1/channels/${encodeURIComponent(channel)}/editorial-projects/${encodeURIComponent(project)}`;
    function clearPreview() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null; el("preview").removeAttribute("src"); el("preview").hidden = true;
    }
    function controls() {
        el("refresh").disabled = busy;
        el("back").disabled = busy || pages.length < 2;
        el("next").disabled = busy || !more;
        el("play").disabled = busy;
        el("list").querySelectorAll("button").forEach(button => { button.disabled = busy; });
    }
    function status(text) { el("status").textContent = text; }
    function reset() {
        generation++; request++; channel = ""; project = "";
        pages = [null]; rows = []; more = false; loaded = false; busy = false; selected = null;
        clearPreview(); el("").hidden = true; el("").open = false;
        el("detail").hidden = true; el("list").replaceChildren(); status(""); controls();
    }
    function context(nextChannel, nextProject) {
        if (channel === nextChannel && project === nextProject) return;
        reset(); channel = nextChannel; project = nextProject; el("").hidden = false;
    }
    async function list(nextPages = pages) {
        if (!project || busy) return;
        const fence = generation, ticket = ++request;
        busy = true; controls(); status("Loading work history…");
        const cursor = nextPages.at(-1);
        try {
            const query = new URLSearchParams({limit: "21"});
            if (cursor) query.set("before", cursor);
            const result = await api(`${base()}/runs?${query}`);
            if (fence !== generation || ticket !== request) return;
            pages = nextPages; more = result.length > 20; rows = result.slice(0, 20); loaded = true;
            el("list").innerHTML = rows.map(row => `<article class="item"><div><h4>${esc(labels[row.target] || "Editorial work")}</h4><p>${esc(readable(row.status))} · Input revision ${esc(row.input_revision ?? 0)} · Attempt ${esc(row.attempt)} · ${esc(date(row.created_at))}</p></div><button type="button" class="mini" data-history-run="${esc(row.editorial_run_id)}">Inspect saved work</button></article>`).join("");
            status(rows.length ? `History page ${pages.length}. Newest work first. Refresh history to include newly started work.` : "No saved work on this page. Refresh history to check again.");
        } catch (error) {
            if (fence === generation && ticket === request) status(`History unavailable: ${error.message}. Your place is retained. Use Refresh history or try the page again.`);
        } finally {
            if (fence === generation && ticket === request) { busy = false; controls(); }
        }
    }
    function showScript(revision) {
        if (!revision) { el("script").textContent = "This work began from the brief before a script revision existed."; return; }
        const draft = revision.draft || {};
        el("script").innerHTML = `<p>Saved revision ${esc(revision.revision)} · ${esc(date(revision.created_at))}</p>`
            + (draft.script || []).map((beat, index) => `<article class="item"><div><h4>Beat ${index + 1} · ${esc(beat.role)}</h4><p>${esc(beat.narration)}</p><p>${esc(beat.visual_intent)}</p></div></article>`).join("")
            + (draft.claims || []).map(claim => `<article class="item"><div><p>${esc(claim.text)}</p><small>${esc(claim.classification)} · ${esc(claim.verification)} · ${esc(claim.verification_note)}</small></div></article>`).join("")
            + (draft.sources || []).map(source => {
                let url; try { url = new URL(source.url); } catch { return ""; }
                if (!["https:", "http:"].includes(url.protocol)) return "";
                return `<blockquote>${esc(source.excerpt)}<footer><a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a></footer></blockquote>`;
            }).join("");
    }
    async function inspect(id) {
        if (!project || busy) return;
        const fence = generation, ticket = ++request;
        busy = true; controls(); clearPreview(); selected = null; el("detail").hidden = true;
        status("Loading selected work…");
        try {
            const row = await api(`${base()}/runs/${encodeURIComponent(id)}`);
            if (fence !== generation || ticket !== request) return;
            const revision = row.artifacts?.saved_revision || row.input_revision;
            const [script, review] = await Promise.all([
                revision ? api(`${base()}/revisions/${revision}`).catch(error => ({error: error.message})) : null,
                row.target === "render" && row.status === "completed" ? api(`${base()}/runs/${encodeURIComponent(id)}/review`).catch(error => ({error: error.message})) : null,
            ]);
            if (fence !== generation || ticket !== request) return;
            selected = row;
            el("title").textContent = `${labels[row.target] || "Editorial work"} · ${date(row.created_at)}`;
            el("result").textContent = `${readable(row.status)} · ${readable(row.stage)} · Input revision ${row.input_revision ?? 0} · Attempt ${row.attempt}. ${row.error || ""}`;
            if (script?.error) el("script").textContent = `Saved script unavailable: ${script.error}. Inspect this work again to retry.`;
            else showScript(script);
            el("review").replaceChildren();
            if (review) {
                const reviewLabels = {approve: "Currently approved", invalidated: "Previous approval is no longer valid", unreviewed: "Awaiting review", request_changes: "Changes requested"};
                el("review").innerHTML = `<p>${esc(review.error ? `Review status unavailable: ${review.error}` : `${reviewLabels[review.status] || "Review required"}. ${review.blocker || ""}`)}</p>`
                    + (review.reviews || []).map(item => `<article class="item"><div><p>${esc(item.decision === "approve" ? "Approval recorded" : "Changes requested")} · ${esc(item.actor)} · ${esc(date(item.created_at))}</p><p>${esc(item.note)}</p></div></article>`).join("");
            }
            el("play").hidden = row.target !== "render" || row.status !== "completed";
            el("receipts").textContent = JSON.stringify(row.artifacts || {}, null, 2);
            el("detail").hidden = false; el("title").focus();
            status("Saved work loaded for inspection. Current script edits are unchanged.");
        } catch (error) {
            if (fence === generation && ticket === request) status(`Selected work unavailable: ${error.message}. Inspect it again to retry.`);
        } finally {
            if (fence === generation && ticket === request) { busy = false; controls(); }
        }
    }
    async function play() {
        if (!selected || busy) return;
        const fence = generation, ticket = ++request;
        busy = true; controls(); clearPreview(); status("Checking access and loading selected preview…");
        try {
            const blob = await apiBlob(`${base()}/runs/${encodeURIComponent(selected.editorial_run_id)}/preview`);
            if (fence !== generation || ticket !== request) return;
            previewUrl = URL.createObjectURL(blob); el("preview").src = previewUrl; el("preview").hidden = false;
            status("Selected preview loaded. Historical review receipts do not authorize publication.");
        } catch (error) {
            if (fence === generation && ticket === request) status(`Preview unavailable: ${error.message}. Saved work and current edits are retained.`);
        } finally {
            if (fence === generation && ticket === request) { busy = false; controls(); }
        }
    }
    function init(transport, blobTransport) {
        api = transport; apiBlob = blobTransport;
        el("").addEventListener("toggle", () => { if (el("").open && !loaded) void list(); });
        el("refresh").addEventListener("click", () => void list([null]));
        el("back").addEventListener("click", () => void list(pages.slice(0, -1)));
        el("next").addEventListener("click", () => void list([...pages, rows.at(-1)?.editorial_run_id]));
        el("list").addEventListener("click", event => { const button = event.target.closest("[data-history-run]"); if (button) void inspect(button.dataset.historyRun); });
        el("play").addEventListener("click", () => void play());
    }
    return {init, context, reset};
})();
