/* Katcha Clip Studio: non-destructive operator review and correction. */
const $ = (id) => document.getElementById(id);
const query = new URLSearchParams(location.search);
const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    channel: query.get("channel") || sessionStorage.getItem("katcha.channel") || "",
    episodeId: query.get("episode") || "",
    channels: [],
    episodes: [],
    detail: null,
    brands: [],
    blueprints: [],
    providerStatus: [],
    elevenlabsStatus: null,
    invideoHandoffs: [],
    selectedPosition: null,
    edits: new Map(),
    monitorMode: "render",
    renderUrl: null,
    mediaUrls: new Map(),
    stagedBrand: null,
    localLogoUrl: null,
    logo: { enabled: false, storage_key: null, x_percent: 88, y_percent: 8, width_percent: 13, opacity: 0.9 },
};
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" })[c]);
const clone = (value) => JSON.parse(JSON.stringify(value));
const lineList = (value) => String(value || "").split("\n").map((row) => row.trim()).filter(Boolean).slice(0, 12);
function message(value, error=false) { $("message").textContent=value; $("message").classList.toggle("error", error); }
function remember() {
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
    if (state.channel) sessionStorage.setItem("katcha.channel", state.channel);
}
async function request(path, options={}) {
    return fetch(path, {
        ...options,
        headers: {
            ...(options.body ? {"Content-Type":"application/json"} : {}),
            ...(state.token ? {Authorization:`Bearer ${state.token}`} : {}),
            ...options.headers,
        },
    });
}
async function api(path, options={}) {
    const response=await request(path, options);
    if (!response.ok) {
        let body; try { body=await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`);
    }
    return response.json();
}
function channelPath(path="") { return `/v1/channels/${encodeURIComponent(state.channel)}${path}`; }
function formatTime(seconds) {
    const value=Math.max(0, Number(seconds)||0), mins=Math.floor(value/60), secs=Math.floor(value%60), ms=Math.floor((value%1)*1000);
    return `${String(mins).padStart(2,"0")}:${String(secs).padStart(2,"0")}.${String(ms).padStart(3,"0")}`;
}
function revokeMedia() {
    if (state.renderUrl) URL.revokeObjectURL(state.renderUrl);
    state.renderUrl=null;
    for (const url of state.mediaUrls.values()) URL.revokeObjectURL(url);
    state.mediaUrls.clear();
}
async function blobUrl(path, cacheKey) {
    if (state.mediaUrls.has(cacheKey)) return state.mediaUrls.get(cacheKey);
    const response=await request(path);
    if (!response.ok) {
        let body; try { body=await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Media unavailable (${response.status})`);
    }
    const url=URL.createObjectURL(await response.blob());
    state.mediaUrls.set(cacheKey, url);
    return url;
}
function episodeRow() { return state.episodes.find((row)=>String(row.id)===String(state.episodeId)); }
function manifest() { return state.detail?.episode?.render_manifest?.version === "ranked-episode-render-v1" ? state.detail.episode.render_manifest : null; }
function selectedBaseItem() { return manifest()?.items?.find((item)=>Number(item.position)===Number(state.selectedPosition)) || null; }
function sourceMaxFor(item) {
    const detailItem=state.detail?.items?.find((row)=>String(row.clip_id)===String(item?.source?.clip_id));
    return Number(detailItem?.analysis_snapshot?.duration_seconds || item?.source?.source_end_seconds || 0);
}
function draftItem(base) {
    const edit=state.edits.get(Number(base.position));
    if (!edit) return clone(base);
    const item=clone(base);
    item.source.source_start_seconds=Number(edit.source_start_seconds);
    item.source.duration_seconds=Number(edit.duration_seconds);
    item.source.source_end_seconds=Number(edit.source_start_seconds)+Number(edit.duration_seconds);
    item.source.native_audio_policy=edit.native_audio_policy;
    item.source.audio_volume=Number(edit.audio_volume);
    item.source.narration_duck_volume=Number(edit.narration_duck_volume);
    item.transition_before=edit.transition_before;
    return item;
}
function draftTimeline() {
    const base=manifest();
    if (!base) return null;
    let cursor=0;
    const items=base.items.map((raw)=>{
        const item=draftItem(raw);
        item.timeline_start_seconds=cursor;
        cursor += Number(item.source.duration_seconds);
        item.timeline_end_seconds=cursor;
        return item;
    });
    let narrationCursor=.08;
    const overlays=[...(base.overlays||[])].sort((a,b)=>a.sequence-b.sequence).map((raw)=>{
        const overlay=clone(raw), duration=Number(overlay.duration_seconds);
        const target=items.find((item)=>Number(item.position)===Number(overlay.position));
        const start=target?.timeline_start_seconds ?? 0, end=target?.timeline_end_seconds ?? cursor;
        let requested;
        if (overlay.placement==="opening") requested=.08;
        else if (["reveal","pre_clip"].includes(overlay.placement)) requested=start+.08;
        else if (overlay.placement==="post_clip") requested=Math.max(start+.1,end-duration-.08);
        else if (overlay.placement==="transition") requested=Math.max(start+.1,end-duration-.04);
        else if (overlay.placement==="closing") requested=cursor+.05;
        else if (overlay.placement==="interaction") requested=Math.max(cursor+.05,narrationCursor);
        else requested=narrationCursor;
        overlay.start_seconds=Math.max(requested,narrationCursor);
        narrationCursor=overlay.start_seconds+duration+.06;
        return overlay;
    });
    const interaction=overlays.find((row)=>row.placement==="interaction");
    const endStart=interaction ? interaction.start_seconds : (base.end_card?.prompt ? Math.max(cursor,narrationCursor)+.05 : Math.max(cursor,narrationCursor));
    const endDuration=interaction ? Math.max(2.4,Number(interaction.duration_seconds)+.35) : (base.end_card?.prompt ? Math.max(2.4,Number(base.end_card?.duration_seconds||2.4)) : .35);
    const duration=Math.max(cursor,narrationCursor,endStart+endDuration)+.1;
    return {items,overlays,duration};
}
function renderDirtyState() {
    const dirty=state.edits.size>0;
    $("save-state").textContent=dirty ? `${state.edits.size} CLIP EDIT${state.edits.size===1?"":"S"}` : "NO CLIP EDITS";
    $("save-state").classList.toggle("dirty",dirty); $("save-state").classList.toggle("clean",!dirty);
}
function renderProject() {
    const row=episodeRow();
    $("episode-status").textContent=row ? String(row.status||"unknown").replaceAll("_"," ").toUpperCase() : "NO EPISODE";
    $("episode-meta").textContent=row ? `Generation ${row.generation} · ${row.stage} · ${row.edit_blueprint_key||"channel default"}` : "Select a production to inspect.";
    $("monitor-title").textContent=row?.premise || "No episode loaded";
    $("render-edits").disabled=!manifest();
}
function clipRows() {
    const m=manifest();
    if (m) return m.items;
    return (state.detail?.items||[]).map((row)=>({position:row.position,role:row.role,source:{clip_id:row.clip_id,duration_seconds:Number(row.analysis_snapshot?.duration_seconds||0)}}));
}
function renderClipList() {
    const rows=clipRows(); $("clip-count").textContent=String(rows.length);
    $("clip-list").innerHTML=rows.length ? rows.map((row)=>{
        const active=Number(row.position)===Number(state.selectedPosition), dirty=state.edits.has(Number(row.position));
        const d=draftItem(row);
        return `<button class="clip-row ${active?"active":""}" type="button" data-position="${escapeHTML(row.position)}"><span class="clip-rank">#${escapeHTML(row.position)}</span><span><strong>${escapeHTML(String(row.role||"clip").replaceAll("_"," "))}</strong><small>${Number(d.source?.duration_seconds||0).toFixed(2)}s · ${escapeHTML(String(d.source?.native_audio_policy||"source"))}${dirty?' · <span class="dirty-mark">EDITED</span>':""}</small></span></button>`;
    }).join("") : '<div class="studio-empty">This episode has no source clips to inspect.</div>';
}
function renderClipInspector() {
    const base=selectedBaseItem(), controls=$("clip-controls");
    if (!base) {
        controls.disabled=true; $("clip-title").textContent=state.selectedPosition ? `Clip #${state.selectedPosition}` : "Select a clip";
        $("clip-context").textContent=manifest() ? "Choose a timeline clip to edit." : "Source preview is available, but precise controls unlock after the render manifest is frozen.";
        return;
    }
    const item=draftItem(base), max=sourceMaxFor(base);
    controls.disabled=false;
    $("clip-title").textContent=`#${base.position} · ${String(base.role||"clip").replaceAll("_"," ")}`;
    $("clip-context").textContent=`Clip ${String(base.source.clip_id).slice(0,8)} · timeline ${formatTime(item.timeline_start_seconds)}`;
    $("clip-start").value=Number(item.source.source_start_seconds).toFixed(2);
    $("clip-duration").value=Number(item.source.duration_seconds).toFixed(2);
    $("clip-transition").value=item.transition_before;
    $("clip-audio-policy").value=item.source.native_audio_policy;
    $("clip-volume").value=item.source.audio_volume; $("clip-duck").value=item.source.narration_duck_volume;
    $("clip-volume-out").textContent=`${Math.round(Number(item.source.audio_volume)*100)}%`;
    $("clip-duck-out").textContent=`${Math.round(Number(item.source.narration_duck_volume)*100)}%`;
    $("clip-end").textContent=`${(Number(item.source.source_start_seconds)+Number(item.source.duration_seconds)).toFixed(2)}s`;
    $("clip-max").textContent=max ? `${max.toFixed(2)}s` : "unknown";
}
function readClipEdit() {
    const base=selectedBaseItem(); if (!base) return;
    const edit={
        position:Number(base.position),
        source_start_seconds:Number($("clip-start").value),
        duration_seconds:Number($("clip-duration").value),
        transition_before:$("clip-transition").value,
        native_audio_policy:$("clip-audio-policy").value,
        audio_volume:Number($("clip-volume").value),
        narration_duck_volume:Number($("clip-duck").value),
    };
    state.edits.set(edit.position,edit);
    renderClipInspector(); renderClipList(); renderTimeline(); renderDirtyState();
    if (state.monitorMode==="source") {
        const video=$("monitor-video");
        const end=edit.source_start_seconds+edit.duration_seconds;
        if (video.currentTime<edit.source_start_seconds || video.currentTime>end) video.currentTime=edit.source_start_seconds;
    }
}
async function selectClip(position) {
    state.selectedPosition=Number(position); renderClipList(); renderClipInspector(); renderTimeline();
    state.monitorMode="source"; await showMonitor();
}
async function sourceUrlForSelected() {
    const row=clipRows().find((item)=>Number(item.position)===Number(state.selectedPosition));
    const clipId=row?.source?.clip_id; if (!clipId) return null;
    return blobUrl(`/v1/studio/clips/${encodeURIComponent(clipId)}/media`,`clip:${clipId}`);
}
async function showMonitor() {
    const video=$("monitor-video"), empty=$("monitor-empty");
    $("show-render").classList.toggle("active",state.monitorMode==="render");
    $("show-source").classList.toggle("active",state.monitorMode==="source");
    let url=null;
    try {
        if (state.monitorMode==="render") url=state.renderUrl;
        else url=await sourceUrlForSelected();
    } catch (error) { message(error.message,true); }
    if (!url && state.monitorMode==="render" && state.selectedPosition) {
        state.monitorMode="source"; return showMonitor();
    }
    if (url) {
        video.src=url; empty.hidden=true;
        if (state.monitorMode==="source") {
            const item=selectedBaseItem() ? draftItem(selectedBaseItem()) : null;
            video.onloadedmetadata=()=>{ if (item) video.currentTime=Number(item.source.source_start_seconds)||0; };
        } else video.onloadedmetadata=null;
    } else {
        video.removeAttribute("src"); video.load(); empty.hidden=false;
    }
    renderLogoOverlay();
}
async function loadRenderedMedia() {
    state.renderUrl=null;
    if (!state.episodeId) return;
    try {
        state.renderUrl=await blobUrl(`/v1/studio/episodes/${encodeURIComponent(state.episodeId)}/media`,`render:${state.episodeId}`);
    } catch {}
}
function renderTimeline() {
    const timeline=draftTimeline(), canvas=$("timeline-canvas");
    if (!timeline) { canvas.innerHTML='<div class="studio-empty">Katcha has not frozen a render manifest for this episode yet.</div>'; $("timeline-duration").textContent="00:00.000"; return; }
    const total=Math.max(timeline.duration,.1), tickStep=total>40?10:5, ticks=[];
    for(let t=0;t<=total+.001;t+=tickStep) ticks.push(`<span style="left:${Math.min(100,t/total*100)}%">${Math.round(t)}s</span>`);
    const videos=timeline.items.map((item)=>`<button class="timeline-block video ${Number(item.position)===Number(state.selectedPosition)?"active":""}" type="button" data-position="${item.position}" style="left:${item.timeline_start_seconds/total*100}%;width:${Number(item.source.duration_seconds)/total*100}%">#${item.position} · ${escapeHTML(item.role)}</button>`).join("");
    const audio=timeline.overlays.map((row)=>`<div class="timeline-block audio" title="${escapeHTML(row.text||row.placement)}" style="left:${Number(row.start_seconds)/total*100}%;width:${Number(row.duration_seconds)/total*100}%">${escapeHTML(row.placement)}</div>`).join("");
    canvas.innerHTML=`<div class="ruler">${ticks.join("")}</div><div class="track-row"><div class="track-label">V1</div><div class="track-lane">${videos}</div></div><div class="track-row"><div class="track-label">VOICE</div><div class="track-lane">${audio}</div></div><div id="playhead" class="playhead" style="left:52px"></div>`;
    $("timeline-duration").textContent=formatTime(total);
}
function updatePlayhead() {
    const timeline=draftTimeline(); if (!timeline) return;
    const video=$("monitor-video"), canvas=$("timeline-canvas"), head=$("playhead"); if (!head) return;
    const lane=canvas.querySelector(".track-lane"); if (!lane) return;
    let seconds=video.currentTime;
    if (state.monitorMode==="source") {
        const item=selectedBaseItem()?draftItem(selectedBaseItem()):null;
        if (item) seconds=Number(item.timeline_start_seconds)+(video.currentTime-Number(item.source.source_start_seconds));
    }
    const x=lane.offsetLeft+Math.max(0,Math.min(1,seconds/timeline.duration))*lane.clientWidth;
    head.style.left=`${x}px`; $("timecode").textContent=formatTime(seconds);
    if (state.monitorMode==="source") {
        const item=selectedBaseItem()?draftItem(selectedBaseItem()):null;
        if (item && video.currentTime>Number(item.source.source_end_seconds)) { video.pause(); video.currentTime=Number(item.source.source_start_seconds); }
    }
}
function activeBlueprint() {
    const key=state.detail?.episode?.edit_blueprint_key;
    return state.blueprints.find((row)=>row.is_active && row.blueprint_key===key)
        || state.blueprints.find((row)=>row.is_default)
        || state.blueprints.find((row)=>row.is_active)
        || null;
}
function latestInVideoHandoff() {
    return state.invideoHandoffs[0] || null;
}
function renderExternalPanel() {
    const eleven = state.providerStatus.find((row)=>row.provider==="elevenlabs");
    const invideo = state.providerStatus.find((row)=>row.provider==="invideo");
    const elevenState=$("elevenlabs-state"), invideoState=$("invideo-state");
    if (elevenState) {
        const connected=Boolean(state.elevenlabsStatus?.connected);
        elevenState.textContent=connected ? "CONNECTED" : eleven?.configured ? "CONFIGURED" : "NOT CONFIGURED";
        elevenState.classList.toggle("ready",connected);
        elevenState.classList.toggle("warning",!connected);
        $("elevenlabs-detail").textContent=state.elevenlabsStatus?.detail || eleven?.detail || "ElevenLabs status unavailable.";
        $("elevenlabs-voice").textContent=state.elevenlabsStatus?.voice_name
            ? `${state.elevenlabsStatus.voice_name} · ${state.elevenlabsStatus.model_id}`
            : state.elevenlabsStatus?.voice_id || "—";
        $("elevenlabs-plan").textContent=state.elevenlabsStatus?.subscription?.tier || "—";
        const sub=state.elevenlabsStatus?.subscription;
        $("elevenlabs-usage").textContent=sub
            ? `${Number(sub.character_count||0).toLocaleString()} / ${Number(sub.character_limit||0).toLocaleString()} characters`
            : "—";
    }
    if (invideoState) {
        invideoState.textContent=invideo?.mode==="manual_bridge" ? "BRIDGE READY" : "UNAVAILABLE";
        invideoState.classList.toggle("ready",Boolean(invideo?.configured));
        invideoState.classList.toggle("warning",!invideo?.configured);
    }
    const handoff=latestInVideoHandoff();
    $("prepare-invideo").disabled=!state.episodeId;
    $("download-invideo").disabled=!handoff;
    $("import-invideo").disabled=!handoff || !$("invideo-output").files?.[0]
        || ["adopted","cancelled"].includes(handoff.status);
    $("adopt-invideo").disabled=!handoff || handoff.status!=="output_imported";
    $("invideo-handoff").innerHTML=handoff
        ? `<strong>Generation ${escapeHTML(handoff.generation)} · ${escapeHTML(String(handoff.status).replaceAll("_"," ").toUpperCase())}</strong><br><span>Handoff ${escapeHTML(String(handoff.id).slice(0,8))}${handoff.external_project_id ? ` · project ${escapeHTML(handoff.external_project_id)}` : ""}</span>`
        : "No handoff prepared for this episode.";
}
async function loadProviderStatus() {
    try {
        state.providerStatus=await api("/v1/integrations/providers");
        const eleven=state.providerStatus.find((row)=>row.provider==="elevenlabs");
        state.elevenlabsStatus=eleven?.configured
            ? await api("/v1/integrations/elevenlabs/status")
            : null;
    } catch(error) {
        state.elevenlabsStatus={connected:false,detail:error.message};
    }
    renderExternalPanel();
}
async function loadInVideoHandoffs() {
    state.invideoHandoffs=[];
    if (!state.episodeId) { renderExternalPanel(); return; }
    try {
        state.invideoHandoffs=await api(
            `/v1/integrations/invideo/handoffs?source_type=short_episode&source_id=${encodeURIComponent(state.episodeId)}&limit=20`
        );
    } catch(error) {
        message(error.message,true);
    }
    renderExternalPanel();
}
async function prepareInVideo() {
    if (!state.episodeId) throw new Error("Select an episode first.");
    const row=await api("/v1/integrations/invideo/handoffs",{
        method:"POST",
        body:JSON.stringify({
            source_type:"short_episode",
            source_id:state.episodeId,
            actor:"clip-studio",
            note:$("invideo-note").value.trim()||null,
        }),
    });
    state.invideoHandoffs=[row,...state.invideoHandoffs];
    renderExternalPanel();
    message("InVideo handoff prepared. Download the package and open it in InVideo.");
}
async function downloadInVideoPackage() {
    const row=latestInVideoHandoff();
    if (!row) throw new Error("Prepare an InVideo handoff first.");
    const response=await request(`/v1/integrations/invideo/handoffs/${encodeURIComponent(row.id)}/package`);
    if (!response.ok) {
        let body; try { body=await response.json(); } catch {}
        throw new Error(typeof body?.detail==="string" ? body.detail : `Package download failed (${response.status})`);
    }
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement("a");
    link.href=url;
    link.download=`katcha-invideo-${row.id}.zip`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
    message("InVideo handoff package downloaded.");
}
async function importInVideoOutput() {
    const row=latestInVideoHandoff(), file=$("invideo-output").files?.[0];
    if (!row) throw new Error("Prepare an InVideo handoff first.");
    if (!file) throw new Error("Choose the MP4 returned by InVideo.");
    const form=new FormData();
    form.append("file",file,file.name);
    const projectId=$("invideo-project-id").value.trim();
    if (projectId) form.append("external_project_id",projectId);
    form.append("actor","clip-studio");
    const response=await fetch(
        `/v1/integrations/invideo/handoffs/${encodeURIComponent(row.id)}/output`,
        {
            method:"POST",
            headers:state.token ? {Authorization:`Bearer ${state.token}`} : {},
            body:form,
        },
    );
    if (!response.ok) {
        let body; try { body=await response.json(); } catch {}
        throw new Error(typeof body?.detail==="string" ? body.detail : `InVideo output import failed (${response.status})`);
    }
    const updated=await response.json();
    state.invideoHandoffs=[updated,...state.invideoHandoffs.filter((item)=>item.id!==updated.id)];
    renderExternalPanel();
    message("InVideo MP4 imported and verified. Review it, then adopt it when ready.");
}
async function adoptInVideoOutput() {
    const row=latestInVideoHandoff();
    if (!row) throw new Error("No InVideo handoff is available.");
    const updated=await api(
        `/v1/integrations/invideo/handoffs/${encodeURIComponent(row.id)}/adopt`,
        {method:"POST",body:JSON.stringify({actor:"clip-studio"})},
    );
    state.invideoHandoffs=[updated,...state.invideoHandoffs.filter((item)=>item.id!==updated.id)];
    await loadEpisode();
    renderExternalPanel();
    message("InVideo edit adopted into Katcha. It is now the episode render under review.");
}
function renderAiPanel() {
    const row=activeBlueprint(), guidance=row?.contract?.ai_guidance || {};
    $("ai-recipe").textContent=row ? `${row.blueprint_metadata?.display_name||row.blueprint_key} · active v${row.version} · loaded episode froze v${state.detail?.episode?.edit_blueprint_version||"—"}` : "No active recipe";
    $("ai-strength").value=guidance.instruction_strength||"balanced";
    $("ai-order").checked=guidance.preserve_clip_order!==false; $("ai-native").checked=guidance.prefer_native_moments!==false;
    $("ai-always").value=(guidance.always_rules||[]).join("\n"); $("ai-never").value=(guidance.never_rules||[]).join("\n"); $("ai-notes").value=guidance.operator_notes||"";
    $("save-ai").disabled=!row;
}
async function saveAiRules() {
    const row=activeBlueprint(); if (!row) throw new Error("No active editing recipe is available.");
    const contract=clone(row.contract);
    contract.ai_guidance={
        instruction_strength:$("ai-strength").value,
        preserve_clip_order:$("ai-order").checked,
        prefer_native_moments:$("ai-native").checked,
        always_rules:lineList($("ai-always").value),
        never_rules:lineList($("ai-never").value),
        operator_notes:$("ai-notes").value.trim(),
    };
    const created=await api(channelPath("/edit-blueprints"),{method:"POST",body:JSON.stringify({
        contract, actor:"clip-studio", set_default:Boolean(row.is_default),
        display_name:row.blueprint_metadata?.display_name||row.blueprint_key,
        description:row.blueprint_metadata?.description||"",
    })});
    state.blueprints=await api(channelPath("/edit-blueprints")); renderAiPanel();
    message(`AI boundaries saved as ${created.blueprint_key} v${created.version}. Future videos will inherit them.`);
}
function activeBrand() { return state.brands.find((row)=>row.is_active)||null; }
function readLogoInputs() {
    state.logo.enabled=$("logo-enabled").checked;
    state.logo.x_percent=Number($("logo-x").value); state.logo.y_percent=Number($("logo-y").value);
    state.logo.width_percent=Number($("logo-width").value); state.logo.opacity=Number($("logo-opacity").value);
    $("logo-x-out").textContent=`${Math.round(state.logo.x_percent)}%`; $("logo-y-out").textContent=`${Math.round(state.logo.y_percent)}%`;
    $("logo-width-out").textContent=`${Math.round(state.logo.width_percent)}%`; $("logo-opacity-out").textContent=`${Math.round(state.logo.opacity*100)}%`;
    renderLogoOverlay();
}
function renderLogoOverlay() {
    const image=$("logo-overlay"), label=$("logo-preview-label");
    const src=state.localLogoUrl || image.dataset.activeSrc || "";
    if (!state.logo.enabled || !src) { image.hidden=true; label.hidden=true; return; }
    image.src=src; image.style.left=`${state.logo.x_percent}%`; image.style.top=`${state.logo.y_percent}%`;
    image.style.width=`${state.logo.width_percent}%`; image.style.opacity=state.logo.opacity; image.hidden=false; label.hidden=false;
}
async function hydrateBrandPanel() {
    const row=activeBrand(), logo=clone(row?.contract?.visual?.logo||{});
    state.logo={enabled:Boolean(logo.enabled),storage_key:logo.storage_key||null,x_percent:Number(logo.x_percent??88),y_percent:Number(logo.y_percent??8),width_percent:Number(logo.width_percent??13),opacity:Number(logo.opacity??.9)};
    state.stagedBrand=null; $("activate-logo").disabled=true;
    $("brand-version").textContent=row ? `${row.brand_key} · live v${row.version}` : "No active brand";
    $("logo-enabled").checked=state.logo.enabled; $("logo-x").value=state.logo.x_percent; $("logo-y").value=state.logo.y_percent; $("logo-width").value=state.logo.width_percent; $("logo-opacity").value=state.logo.opacity;
    readLogoInputs();
    delete $("logo-overlay").dataset.activeSrc;
    if (state.logo.enabled && state.logo.storage_key) {
        try {
            const url=await blobUrl(`/v1/studio/channels/${encodeURIComponent(state.channel)}/logo/media`,`logo:${state.channel}:${row?.version}`);
            $("logo-overlay").dataset.activeSrc=url; renderLogoOverlay();
        } catch {}
    }
}
function fileBase64(file) {
    return new Promise((resolve,reject)=>{ const reader=new FileReader(); reader.onerror=()=>reject(reader.error); reader.onload=()=>resolve(String(reader.result).split(",",2)[1]); reader.readAsDataURL(file); });
}
async function stageLogo() {
    const active=activeBrand(); if (!active) throw new Error("No active channel brand is available.");
    const file=$("logo-file").files[0];
    let storageKey=state.logo.storage_key;
    if (file) {
        if (!["image/png","image/webp"].includes(file.type)) throw new Error("Use a PNG or WebP logo.");
        if (file.size>2*1024*1024) throw new Error("Logo must be 2 MB or smaller.");
        const uploaded=await api(`/v1/studio/channels/${encodeURIComponent(state.channel)}/logo`,{method:"POST",body:JSON.stringify({content_type:file.type,data_base64:await fileBase64(file)})});
        storageKey=uploaded.storage_key;
    }
    if (state.logo.enabled && !storageKey) throw new Error("Choose a logo image before enabling the logo.");
    const nextVersion=Math.max(...state.brands.map((row)=>Number(row.version)),0)+1, contract=clone(active.contract);
    contract.version=nextVersion; contract.visual={...(contract.visual||{}),version:nextVersion,logo:{
        enabled:state.logo.enabled,storage_key:storageKey||null,x_percent:state.logo.x_percent,y_percent:state.logo.y_percent,width_percent:state.logo.width_percent,opacity:state.logo.opacity,
    }};
    state.stagedBrand=await api(channelPath("/brands"),{method:"POST",body:JSON.stringify({contract,actor:"clip-studio",hypothesis:"Operator-defined persistent logo placement from Clip Studio"})});
    state.logo.storage_key=storageKey; $("activate-logo").disabled=false;
    $("brand-version").textContent=`${active.brand_key} · live v${active.version} · staged v${state.stagedBrand.version}`;
    message(`Brand v${state.stagedBrand.version} staged. Review the on-monitor logo position, then activate it when ready.`);
}
async function activateLogo() {
    if (!state.stagedBrand) throw new Error("Stage a logo version first.");
    await api(channelPath(`/brands/${state.stagedBrand.version}/activate`),{method:"POST",body:JSON.stringify({actor:"clip-studio"})});
    state.brands=await api(channelPath("/brands")); await hydrateBrandPanel();
    message(`Brand v${activeBrand()?.version} is now active for future short-form renders.`);
}
async function renderEditedGeneration() {
    const timeline=draftTimeline(); if (!timeline) throw new Error("This episode does not have a frozen render manifest yet.");
    const edits=timeline.items.map((item)=>({
        position:Number(item.position),source_start_seconds:Number(item.source.source_start_seconds),duration_seconds:Number(item.source.duration_seconds),
        native_audio_policy:item.source.native_audio_policy,audio_volume:Number(item.source.audio_volume),narration_duck_volume:Number(item.source.narration_duck_volume),transition_before:item.transition_before,
    }));
    const result=await api(`/v1/studio/episodes/${encodeURIComponent(state.episodeId)}/render`,{method:"POST",body:JSON.stringify({
        edits,adopt_active_brand:$("adopt-brand").checked,actor:"clip-studio",note:$("render-note").value.trim()||null,
    })});
    state.episodeId=result.child_episode_id; query.set("episode",state.episodeId); query.set("channel",state.channel);
    history.replaceState(null,"",`/studio?${query.toString()}`);
    state.edits.clear();
    await loadChannel(state.episodeId);
    message(`Edited generation created (${String(state.episodeId).slice(0,8)}). Rendering has started.`);
}
async function loadEpisode() {
    revokeMedia(); state.edits.clear(); state.selectedPosition=null; renderDirtyState();
    if (!state.episodeId) { state.detail=null; renderAll(); return; }
    message("Loading edit session…");
    try {
        state.detail=await api(`/v1/short-episodes/${encodeURIComponent(state.episodeId)}`);
        renderAll(); await loadRenderedMedia(); await loadInVideoHandoffs();
        const first=clipRows()[0]; if (first) state.selectedPosition=Number(first.position);
        renderAll(); await hydrateBrandPanel();
        if (state.renderUrl) state.monitorMode="render"; else state.monitorMode="source";
        await showMonitor(); message(manifest() ? "Studio ready. Manual changes will create a new non-destructive generation." : "Episode loaded. Source review is available; clip editing unlocks after a render manifest is frozen.");
    } catch(error){ message(error.message,true); }
}
async function loadChannel(preferredEpisode=null) {
    remember();
    revokeMedia();
    if (state.localLogoUrl) URL.revokeObjectURL(state.localLogoUrl);
    state.localLogoUrl=null;
    state.detail=null; state.edits.clear(); state.selectedPosition=null;
    if (!state.channel) { renderAll(); return; }
    message("Loading channel workspace…");
    try {
        [state.episodes,state.brands,state.blueprints]=await Promise.all([
            api(`/v1/short-episodes?channel_profile_id=${encodeURIComponent(state.channel)}&limit=100`),
            api(channelPath("/brands")),
            api(channelPath("/edit-blueprints")),
        ]);
        $("episode").innerHTML='<option value="">Choose an episode</option>'+state.episodes.map((row)=>`<option value="${escapeHTML(row.id)}">${escapeHTML(row.premise)} · g${escapeHTML(row.generation)} · ${escapeHTML(row.status)}</option>`).join("");
        $("episode").disabled=false;
        const desired=preferredEpisode||state.episodeId;
        if (desired && state.episodes.some((row)=>String(row.id)===String(desired))) state.episodeId=String(desired);
        else state.episodeId=state.episodes.find((row)=>row.render_manifest?.version==="ranked-episode-render-v1")?.id || state.episodes[0]?.id || "";
        $("episode").value=state.episodeId; renderAll(); await loadEpisode();
    } catch(error){ message(error.message,true); }
}
async function connect(token=state.token) {
    state.token=String(token||"").trim(); remember(); $("token").value="";
    try {
        state.channels=await api("/v1/channels");
        $("channel").innerHTML='<option value="">Select a channel</option>'+state.channels.map((row)=>`<option value="${escapeHTML(row.id)}">${escapeHTML(row.profile_metadata?.channel_title||row.profile_metadata?.name||row.id)} · ${escapeHTML(row.status)}</option>`).join("");
        $("channel").disabled=false; $("refresh").disabled=false; $("connection").textContent="CONNECTED"; $("connection").classList.add("online"); $("connect-form").classList.add("connected");
        if (!state.channel || !state.channels.some((row)=>String(row.id)===String(state.channel))) state.channel=state.channels[0]?.id||"";
        $("channel").value=state.channel; await Promise.all([loadProviderStatus(),loadChannel()]);
    } catch(error){ $("connection").textContent="OFFLINE"; $("connection").classList.remove("online"); $("connect-form").classList.remove("connected"); message(error.message,true); }
}
function renderAll() { renderProject(); renderClipList(); renderClipInspector(); renderTimeline(); renderAiPanel(); renderExternalPanel(); renderDirtyState(); }
function switchTab(name) {
    document.querySelectorAll(".inspector-tab").forEach((button)=>button.classList.toggle("active",button.dataset.tab===name));
    ["clip","ai","brand","external"].forEach((key)=>$(`tab-${key}`).hidden=key!==name);
    if (name==="brand") { state.monitorMode="source"; showMonitor(); }
}
document.addEventListener("click",async(event)=>{
    const clip=event.target.closest("[data-position]"); if (clip) { await selectClip(clip.dataset.position); return; }
    const tab=event.target.closest("[data-tab]"); if (tab) { switchTab(tab.dataset.tab); return; }
});
$("connect-form").addEventListener("submit",(event)=>{event.preventDefault();connect($("token").value);});
$("channel").addEventListener("change",()=>{state.channel=$("channel").value;state.episodeId="";remember();loadChannel();});
$("episode").addEventListener("change",()=>{state.episodeId=$("episode").value;query.set("episode",state.episodeId);query.set("channel",state.channel);history.replaceState(null,"",`/studio?${query.toString()}`);loadEpisode();});
$("refresh").addEventListener("click",()=>loadChannel(state.episodeId));
$("show-render").addEventListener("click",()=>{state.monitorMode="render";showMonitor();});
$("show-source").addEventListener("click",()=>{state.monitorMode="source";showMonitor();});
["clip-start","clip-duration","clip-transition","clip-audio-policy","clip-volume","clip-duck"].forEach((id)=>$(id).addEventListener(id.startsWith("clip-")&&["clip-volume","clip-duck","clip-start","clip-duration"].includes(id)?"input":"change",readClipEdit));
$("reset-clip").addEventListener("click",()=>{if(state.selectedPosition!==null){state.edits.delete(Number(state.selectedPosition));renderAll();showMonitor();}});
["logo-enabled","logo-x","logo-y","logo-width","logo-opacity"].forEach((id)=>$(id).addEventListener(id==="logo-enabled"?"change":"input",readLogoInputs));
$("logo-file").addEventListener("change",()=>{const file=$("logo-file").files[0];if(state.localLogoUrl)URL.revokeObjectURL(state.localLogoUrl);state.localLogoUrl=file?URL.createObjectURL(file):null;if(file)$("logo-enabled").checked=true;readLogoInputs();});
$("save-ai").addEventListener("click",async()=>{const b=$("save-ai");b.disabled=true;try{await saveAiRules();}catch(e){message(e.message,true);}finally{b.disabled=false;}});
$("stage-logo").addEventListener("click",async()=>{const b=$("stage-logo");b.disabled=true;try{readLogoInputs();await stageLogo();}catch(e){message(e.message,true);}finally{b.disabled=false;}});
$("activate-logo").addEventListener("click",async()=>{const b=$("activate-logo");b.disabled=true;try{await activateLogo();}catch(e){message(e.message,true);}finally{b.disabled=!state.stagedBrand;}});
$("render-edits").addEventListener("click",async()=>{const b=$("render-edits");b.disabled=true;try{await renderEditedGeneration();}catch(e){message(e.message,true);}finally{b.disabled=!manifest();}});
$("refresh-providers").addEventListener("click",async()=>{const b=$("refresh-providers");b.disabled=true;try{await loadProviderStatus();message("Provider status refreshed.");}catch(e){message(e.message,true);}finally{b.disabled=false;}});
$("prepare-invideo").addEventListener("click",async()=>{const b=$("prepare-invideo");b.disabled=true;try{await prepareInVideo();}catch(e){message(e.message,true);}finally{renderExternalPanel();}});
$("download-invideo").addEventListener("click",async()=>{const b=$("download-invideo");b.disabled=true;try{await downloadInVideoPackage();}catch(e){message(e.message,true);}finally{renderExternalPanel();}});
$("invideo-output").addEventListener("change",renderExternalPanel);
$("import-invideo").addEventListener("click",async()=>{const b=$("import-invideo");b.disabled=true;try{await importInVideoOutput();}catch(e){message(e.message,true);}finally{renderExternalPanel();}});
$("adopt-invideo").addEventListener("click",async()=>{const b=$("adopt-invideo");b.disabled=true;try{await adoptInVideoOutput();}catch(e){message(e.message,true);}finally{renderExternalPanel();}});
$("monitor-video").addEventListener("timeupdate",updatePlayhead);
document.addEventListener("keydown",(event)=>{
    if (["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName)) return;
    const video=$("monitor-video");
    if(event.code==="Space"){event.preventDefault();video.paused?video.play():video.pause();}
    if(event.key==="ArrowLeft"){event.preventDefault();video.currentTime=Math.max(0,video.currentTime-.5);}
    if(event.key==="ArrowRight"){event.preventDefault();video.currentTime=Math.min(video.duration||Infinity,video.currentTime+.5);}
});
window.addEventListener("beforeunload",()=>{revokeMedia();if(state.localLogoUrl)URL.revokeObjectURL(state.localLogoUrl);});
renderAll();
connect(state.token);
