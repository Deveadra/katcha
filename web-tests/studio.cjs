const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn("python3", ["-m", "http.server", "8768", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const requests = [];
let blueprintVersion = 1;
let brandVersion = 1;
let childCreated = false;

const blueprintContract = {
    key: "persona_commentary",
    version: "1.0.0",
    composition: "blueprint_video",
    source_layout: { mode: "full_frame", fit: "contain", background_mode: "blurred_fill", header_height_px: 0 },
    narration: { mode: "persona_voice", required: true, captions_enabled: true, source_audio_policy: "duck", source_audio_volume: 0.35, narration_duck_volume: 0.14 },
    header: { required: false, max_chars: 0, background: "#000000", foreground: "#ffffff", font_size_px: 54, font_weight: 850, horizontal_padding_px: 56 },
    transition: "punch_cut",
    quality: { min_source_seconds: 2, max_duration_seconds: 60, max_narration_ratio: 0.48 },
    ai_guidance: { instruction_strength: "balanced", preserve_clip_order: true, prefer_native_moments: true, always_rules: [], never_rules: [], operator_notes: "" },
};
const brandContract = (version, activeLogo = false) => ({
    brand_key: "ranksnaxx",
    version,
    persona: { key: "youth_host", version: "2.0.0" },
    voice_policy: { direction_key: "grounded_teen_v1", preferred_profiles: ["fixture"] },
    visual: {
        brand_key: "ranksnaxx", version, theme_key: "signal_v1",
        palette: { ink: "#101216", paper: "#F6F3EC", signal_blue: "#5B6CFF", hot_peach: "#FF7657", volt: "#D9FF57" },
        captions: { treatment_key: "impact_clean_v1", font_family: "Arial", font_size_px: 66, font_weight: 900, max_visual_lines: 2, bottom_safe_zone_px: 250 },
        motion: { treatment_key: "restrained_punch_v1", max_punch_scale: 1.08, freeze_frame_max_frames: 8, random_motion_enabled: false },
        end_card: { treatment_key: "verdict_v1", accent_role: "signal_blue", max_question_lines: 3 },
        ...(activeLogo ? { logo: { enabled: true, storage_key: "brands/one/logos/logo.png", x_percent: 84, y_percent: 9, width_percent: 12, opacity: 0.85 } } : {}),
    },
    packaging: { title_family: "ranked_promise_v1", thumbnail_family: "single_focus_v1" },
    interaction: { allowed_rituals: ["rank_appeal"] },
});
const items = [3,2,1].map((position, index) => ({
    position,
    role: position === 3 ? "opener" : position === 1 ? "payoff" : "build",
    countdown_label: `#${position}`,
    timeline_start_seconds: index * 6,
    timeline_end_seconds: (index + 1) * 6,
    transition_before: position === 1 ? "flash" : "cut",
    source: {
        clip_id: `00000000-0000-0000-0000-00000000000${position}`,
        storage_key: `raw/clip-${position}.mp4`,
        source_start_seconds: 0, source_end_seconds: 6, duration_seconds: 6,
        width: 1080, height: 1920, native_audio_policy: "duck", audio_volume: 0.35, narration_duck_volume: 0.16,
    },
}));
const manifest = {
    version: "ranked-episode-render-v1",
    short_episode_id: "10000000-0000-0000-0000-000000000001",
    width: 1080, height: 1920, fps: 30, items,
    overlays: [
        { sequence: 0, asset_key: "audio/open.wav", placement: "opening", position: null, clip_id: null, text: "Opening line", start_seconds: 0.08, duration_seconds: 0.8, cues: [] },
        { sequence: 1, asset_key: "audio/end.wav", placement: "interaction", position: null, clip_id: null, text: "Your pick?", start_seconds: 18.1, duration_seconds: 0.8, cues: [] },
    ],
    end_card: { start_seconds: 18.1, duration_seconds: 2.4, prompt: "Your pick?" },
    output_duration_seconds: 20.6,
    output_key: "short-episodes/10000000-0000-0000-0000-000000000001/renders/g1.mp4",
    brand: brandContract(1).visual,
    treatment: { premise: "Three clips worth fixing", format_key: "ranksnaxx_countdown", format_version: "1.0.0", item_count: 3, selected_style: "interactive", ordering_roles: ["opener","build","payoff"], narration_density: 0.1, brand_key: "ranksnaxx", brand_version: 1, trend_opportunity_id: null },
    reaction_events: [],
};
const episode = {
    id: "10000000-0000-0000-0000-000000000001",
    channel_profile_id: "20000000-0000-0000-0000-000000000001",
    premise: "Three clips worth fixing", status: "rendered", stage: "render_verified", generation: 1,
    edit_blueprint_key: "persona_commentary", edit_blueprint_version: 1,
    render_manifest: manifest,
};
const detail = {
    episode,
    items: items.map((item) => ({
        id: `item-${item.position}`, short_episode_id: episode.id, clip_id: item.source.clip_id,
        position: item.position, role: item.role, analysis_snapshot: { duration_seconds: 15 }, acquisition_snapshot: {}, source_snapshot: {},
    })),
    scripts: [], assets: [], reviews: [],
};

(async () => {
    for (let i=0;i<50;i++) { try { await fetch("http://127.0.0.1:8768"); break; } catch { await new Promise((r)=>setTimeout(r,100)); } }
    const browser = await chromium.launch({ headless:true, executablePath:process.env.CHROMIUM_PATH||undefined, args:process.env.CHROMIUM_PATH?["--no-sandbox"]:[] });
    try {
        const page = await browser.newPage({ viewport:{width:1440,height:1000} });
        const errors=[]; page.on("pageerror",(error)=>errors.push(error.message));
        await page.route("**/v1/**",(route)=>{
            const request=route.request(), url=new URL(request.url()); let body=null;
            try { body=request.postDataJSON(); } catch {}
            requests.push({path:url.pathname,method:request.method(),body});
            const json=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
            if (url.pathname==="/v1/channels") return json([{id:"20000000-0000-0000-0000-000000000001",status:"active",profile_metadata:{channel_title:"RankSnaxx"}}]);
            if (url.pathname==="/v1/short-episodes") {
                const rows=[episode];
                if (childCreated) rows.unshift({...episode,id:"10000000-0000-0000-0000-000000000002",generation:2,status:"editorial_approved",stage:"studio_edit_ready",render_manifest:{...manifest,short_episode_id:"10000000-0000-0000-0000-000000000002"}});
                return json(rows);
            }
            if (url.pathname.match(/^\/v1\/short-episodes\/[^/]+$/)) {
                if (url.pathname.endsWith("0002")) return json({...detail,episode:{...episode,id:"10000000-0000-0000-0000-000000000002",generation:2,status:"editorial_approved",stage:"studio_edit_ready",render_manifest:{...manifest,short_episode_id:"10000000-0000-0000-0000-000000000002"}}});
                return json(detail);
            }
            if (url.pathname.endsWith("/brands") && request.method()==="GET") return json([{id:"brand-live",channel_profile_id:"20000000-0000-0000-0000-000000000001",version:brandVersion,brand_key:"ranksnaxx",is_active:true,contract:brandContract(brandVersion,brandVersion>1),brand_metadata:{},created_at:"2026-09-28T00:00:00Z"}]);
            if (url.pathname.endsWith("/edit-blueprints") && request.method()==="GET") return json([{id:"bp-live",channel_profile_id:"20000000-0000-0000-0000-000000000001",blueprint_key:"persona_commentary",version:blueprintVersion,contract_version:"1.0.0",is_active:true,is_default:true,contract:blueprintContract,blueprint_metadata:{display_name:"RankSnaxx commentary",description:"Fixture"},created_at:"2026-09-28T00:00:00Z"}]);
            if (url.pathname.endsWith("/edit-blueprints") && request.method()==="POST") { blueprintVersion+=1; Object.assign(blueprintContract,body.contract); return json({id:"bp-new",blueprint_key:"persona_commentary",version:blueprintVersion,contract_version:"1.0.0",is_active:true,is_default:true,contract:body.contract,blueprint_metadata:{display_name:body.display_name,description:body.description},created_at:"2026-09-28T00:00:00Z"}); }
            if (url.pathname.includes("/studio/clips/") && url.pathname.endsWith("/media")) return route.fulfill({status:200,contentType:"video/mp4",body:Buffer.from("fixture-source-video")});
            if (url.pathname.includes("/studio/episodes/") && url.pathname.endsWith("/media")) return route.fulfill({status:200,contentType:"video/mp4",body:Buffer.from("fixture-render-video")});
            if (url.pathname.endsWith("/logo") && request.method()==="POST") return json({storage_key:"brands/one/logos/logo.png",content_type:"image/png",size_bytes:12},201);
            if (url.pathname.endsWith("/brands") && request.method()==="POST") return json({id:"brand-stage",channel_profile_id:"20000000-0000-0000-0000-000000000001",version:brandVersion+1,brand_key:"ranksnaxx",is_active:false,contract:body.contract,brand_metadata:{},created_at:"2026-09-28T00:00:00Z"},201);
            if (url.pathname.match(/\/brands\/\d+\/activate$/)) { brandVersion+=1; return json({}); }
            if (url.pathname.includes("/logo/media")) return route.fulfill({status:200,contentType:"image/png",body:Buffer.from([0x89,0x50,0x4e,0x47])});
            if (url.pathname.includes("/studio/episodes/") && url.pathname.endsWith("/render") && request.method()==="POST") { childCreated=true; return json({source_episode_id:episode.id,child_episode_id:"10000000-0000-0000-0000-000000000002",workflow_id:"fixture-render",status:"editorial_approved"},202); }
            throw new Error(`Unexpected request: ${request.method()} ${url.pathname}`);
        });

        await page.goto("http://127.0.0.1:8768/studio.html?channel=20000000-0000-0000-0000-000000000001&episode=10000000-0000-0000-0000-000000000001");
        await page.locator("#monitor-title").getByText("Three clips worth fixing").waitFor();
        assert.equal(await page.locator(".clip-row").count(),3);
        assert.match(await page.locator("#timeline-duration").innerText(),/^00:/);

        await page.locator(".clip-row").first().click();
        await page.locator("#clip-duration").fill("5.25");
        await page.locator("#clip-duration").dispatchEvent("input");
        assert.match(await page.locator("#save-state").innerText(),/1 CLIP EDIT/);

        await page.getByRole("button",{name:"AI rules"}).click();
        await page.locator("#ai-strength").selectOption("strict");
        await page.locator("#ai-always").fill("Keep native payoff audio\nLet reactions breathe");
        await page.locator("#ai-never").fill("Narrate over the punchline");
        await page.getByRole("button",{name:"Save as new recipe version"}).click();
        await page.getByText(/AI boundaries saved/).waitFor();
        const aiRequest=requests.find((row)=>row.path.endsWith("/edit-blueprints")&&row.method==="POST");
        assert.equal(aiRequest.body.contract.ai_guidance.instruction_strength,"strict");
        assert.deepEqual(aiRequest.body.contract.ai_guidance.always_rules,["Keep native payoff audio","Let reactions breathe"]);

        await page.getByRole("button",{name:"Branding"}).click();
        await page.locator("#logo-x").fill("80");
        await page.locator("#logo-x").dispatchEvent("input");
        await page.locator("#logo-enabled").check();
        await page.locator("#logo-file").setInputFiles({name:"logo.png",mimeType:"image/png",buffer:Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,1,2,3,4])});
        await page.getByRole("button",{name:"Stage new brand version"}).click();
        await page.getByText(/staged/).waitFor();
        const brandRequest=requests.find((row)=>row.path.endsWith("/brands")&&row.method==="POST");
        assert.equal(brandRequest.body.contract.visual.logo.x_percent,80);
        assert.equal(brandRequest.body.contract.visual.logo.enabled,true);
        await page.getByRole("button",{name:"Activate staged version"}).click();
        await page.getByText(/active for future renders/).waitFor();

        await page.getByRole("button",{name:"Clip"}).click();
        await page.getByRole("button",{name:"Render edited generation"}).click();
        await page.getByText(/Rendering has started/).waitFor();
        const renderRequest=requests.find((row)=>row.path.includes("/studio/episodes/")&&row.path.endsWith("/render")&&row.method==="POST");
        assert.equal(renderRequest.body.edits.length,3);
        assert.equal(renderRequest.body.edits[0].duration_seconds,5.25);
        assert.equal(renderRequest.body.adopt_active_brand,true);

        await page.setViewportSize({width:900,height:900});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(errors,[]);
        console.log("Clip Studio browser test passed");
    } finally { await browser.close(); }
})().catch((error)=>{console.error(error);process.exitCode=1;}).finally(()=>server.kill());
