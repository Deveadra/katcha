const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn(
    "python3",
    [
        "-m",
        "http.server",
        "8768",
        "--bind",
        "127.0.0.1",
        "--directory",
        path.resolve(__dirname, "../src/katcha/web"),
    ],
    { stdio: "ignore" },
);

const now = "2026-09-28T10:00:00Z";
const requests = [];
let browser;
let voicePolicy = {
    channel_profile_id: "channel-1",
    brand_version: 2,
    routing_mode: "inherit",
    preferred_profiles: ["openai_youth_v2", "gemini_youth_v2"],
    primary_provider: "openai",
    fallback_providers: ["gemini"],
};
let brandVersion = 2;
let savedGrowthGoals = {
    objective: "ads_revenue",
    path: "fastest",
    pace: "aggressive",
    target_date: null,
    custom_targets: [],
};

const channel = {
    id: "channel-1",
    youtube_connection_id: "youtube-1",
    status: "active",
    timezone: "America/Chicago",
    active_strategy_version: 2,
    active_automation_version: 1,
    profile_metadata: {
        channel_id: "yt-channel-1",
        channel_title: "Fixture Gaming",
        channel_handle: "@fixturegaming",
    },
    created_at: now,
    updated_at: now,
};

const summary = {
    profile: channel,
    strategy: {
        monthly_base_budget_usd: "50.00",
        monthly_hard_budget_usd: "100.00",
        reinvestment_rate: "0.50",
        reinvestment_cap_usd: "40.00",
        fallback_schedule: [],
        blackout_windows: [],
        routing_policy: { mode: "free_first", quality_floor: "task_default" },
    },
    ranking: {
        sample_count: 12,
        confidence: "0.58",
        validation_metrics: {},
    },
    economics: {
        revenue_usd: "30.00",
        attributed_ai_cost_usd: "11.65",
        contribution_margin_usd: "18.35",
        reinvestable_usd: "9.18",
        base_budget_usd: "50.00",
        hard_budget_usd: "100.00",
        effective_budget_usd: "59.18",
        month_to_date_spend_usd: "11.65",
        reserved_ai_cost_usd: "1.00",
        budget_headroom_usd: "46.53",
        burn_rate_usd_per_day: "0.60",
        projected_month_end_spend_usd: "18.00",
        monetary_scope_available: true,
        details: {},
    },
    edit_performance: {
        publication_count: 8,
        blueprint_group_count: 1,
        aggregate_metrics: [
            {
                group_key: "production|short|fast_commentary@r2",
                publication_count: 8,
                mean_average_view_percentage: 72.4,
                mean_audience_watch_ratio_50pct: 0.68,
            },
        ],
        comparison_status: "ready",
        comparison_summary: {},
    },
    packaging_intelligence: {
        publication_count: 6,
        variant_window_count: 9,
        recommendation_count: 1,
        recommendation_status: "ready",
        recommendations: [
            { recommendation: "Favor concise curiosity-led titles for this format." },
        ],
    },
    schedule: [
        {
            rank: 1,
            weekday: 4,
            hour_local: 18,
            score: "0.81",
            sample_count: 9,
            confidence: "0.74",
            source: "learned",
            recommendation_metadata: {},
        },
    ],
    growth: {
        as_of: "2026-09-28",
        sampled_at: now,
        source: "youtube_api_estimate",
        disclaimer: "YouTube Studio remains the authority for exact YPP eligibility.",
        metrics: {
            subscribers: 620,
            public_uploads_90d: 4,
            estimated_qualified_watch_hours_365d: 1000,
            estimated_qualified_shorts_views_90d: 2500000,
        },
        coverage: { channel_statistics: true, analytics: true },
        benchmarks: {
            effective_date: "2026-09-28",
            next_change: {
                effective_date: "2027-02-01",
                ads_premium: {
                    subscribers: 1000,
                    watch_hours_365d: 8000,
                    shorts_views_90d: 20000000,
                },
            },
        },
        goals: savedGrowthGoals,
        milestones: {
            early_ypp: {
                progress: 0.62,
                thresholds_met_estimate: false,
                requirements: [
                    { metric: "subscribers", current: 620, target: 500, progress: 1, estimated: false },
                    { metric: "public_uploads_90d", current: 4, target: 3, progress: 1, estimated: false },
                ],
                audience_paths: [
                    { metric: "qualified_watch_hours_365d", current: 1000, target: 3000, progress: 0.333, estimated: true },
                    { metric: "qualified_shorts_views_90d", current: 2500000, target: 3000000, progress: 0.833, estimated: true },
                ],
            },
            ads_premium: {
                progress: 0.25,
                thresholds_met_estimate: false,
                requirements: [
                    { metric: "subscribers", current: 620, target: 1000, progress: 0.62, estimated: false },
                ],
                audience_paths: [
                    { metric: "qualified_watch_hours_365d", current: 1000, target: 4000, progress: 0.25, estimated: true },
                    { metric: "qualified_shorts_views_90d", current: 2500000, target: 10000000, progress: 0.25, estimated: true },
                ],
            },
        },
        ai: {
            stage: "pre_ypp",
            selected_path: "shorts",
            pace: "aggressive",
            priority_metrics: ["qualified_shorts_views_90d", "subscribers"],
        },
    },
    automation: {
        level: "review_required",
        version: 1,
        next_level: "auto_approve_low_risk",
        eligible_for_next_level: false,
        evidence: {
            reviewed_items: 12,
            approval_rate: 0.83,
            rejection_rate: 0.08,
            regeneration_rate: 0.09,
            publication_failure_rate: 0.04,
            ranking_confidence: 0.58,
            eligible: false,
            reasons: ["insufficient_reviewed_items"],
        },
    },
};

const publications = [
    {
        id: "publication-1",
        production_id: "production-1",
        compilation_id: null,
        short_episode_id: null,
        youtube_connection_id: "youtube-1",
        workflow_id: "publish-1",
        workflow_attempt: 1,
        analytics_workflow_id: "analytics-1",
        status: "published",
        stage: "complete",
        title: "Fixture launch breakdown",
        description: "Fixture description",
        tags: ["gaming", "xbox"],
        category_id: "20",
        privacy_status: "private",
        publish_at: null,
        notify_subscribers: false,
        made_for_kids: false,
        contains_synthetic_media: false,
        treatment_metadata: {},
        youtube_video_id: "fixture-video",
        upload_offset: 100,
        upload_size: 100,
        processing_status: "succeeded",
        failure_reason: null,
        rejection_reason: null,
        raw_status: {},
        published_at: now,
        error: null,
        created_at: now,
        updated_at: now,
    },
    {
        id: "publication-2",
        production_id: "production-2",
        compilation_id: null,
        short_episode_id: null,
        youtube_connection_id: "youtube-1",
        workflow_id: "publish-2",
        workflow_attempt: 2,
        analytics_workflow_id: "analytics-2",
        status: "failed",
        stage: "upload",
        title: "Fixture failed upload",
        description: "",
        tags: [],
        category_id: "20",
        privacy_status: "private",
        publish_at: null,
        notify_subscribers: false,
        made_for_kids: false,
        contains_synthetic_media: false,
        treatment_metadata: {},
        youtube_video_id: null,
        upload_offset: 0,
        upload_size: null,
        processing_status: null,
        failure_reason: "Fixture upload failure",
        rejection_reason: null,
        raw_status: {},
        published_at: null,
        error: "Fixture upload failure",
        created_at: now,
        updated_at: now,
    },
];

const analytics = [
    {
        snapshot: {
            id: "snapshot-1",
            publication_id: "publication-1",
            sample_key: "fixture",
            sampled_at: now,
            period_start: "2026-09-27",
            period_end: "2026-09-28",
            views: 12450,
            engaged_views: 11000,
            estimated_minutes_watched: "4300",
            average_view_duration: "28.4",
            average_view_percentage: "74.2",
            likes: 824,
            comments: 91,
            shares: 55,
            subscribers_gained: 140,
            subscribers_lost: 8,
            estimated_revenue: "22.40",
            estimated_ad_revenue: "21.00",
            monetized_playbacks: 9000,
            raw_metrics: {},
            raw_monetary: {},
            raw_video: {},
            created_at: now,
        },
        retention: [
            {
                id: "retention-1",
                snapshot_id: "snapshot-1",
                elapsed_video_time_ratio: "0.50",
                audience_watch_ratio: "0.66",
                relative_retention_performance: null,
                raw_row: {},
            },
        ],
    },
];

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:8768");
            break;
        } catch {
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
    }

    browser = await chromium.launch({
        headless: true,
        executablePath: process.env.CHROMIUM_PATH || undefined,
        args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.route("**/v1/**", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        let body = null;
        try {
            body = req.postDataJSON();
        } catch {}
        requests.push({
            path: url.pathname,
            method: req.method(),
            auth: req.headers().authorization,
            body,
        });

        let data;
        if (url.pathname === "/v1/channels" && req.method() === "GET") {
            data = [channel];
        } else if (url.pathname === "/v1/integrations/providers") {
            data = [
                {
                    provider: "elevenlabs",
                    capability: "text_to_speech",
                    configured: true,
                    mode: "api",
                    detail: "Direct ElevenLabs TTS is ready.",
                },
                {
                    provider: "openai",
                    capability: "text_to_speech",
                    configured: true,
                    mode: "api",
                    detail: "OpenAI TTS is available as a channel voice or fallback.",
                },
                {
                    provider: "gemini",
                    capability: "text_to_speech",
                    configured: true,
                    mode: "api",
                    detail: "Gemini TTS is available as a channel voice or fallback.",
                },
                {
                    provider: "invideo",
                    capability: "external_edit",
                    configured: true,
                    mode: "manual_bridge",
                    detail: "Katcha prepares a tracked edit package.",
                },
            ];
        } else if (url.pathname === "/v1/integrations/elevenlabs/verify") {
            data = {
                configured: true,
                reachable: true,
                voice_id_hint: "voic…e123",
                model_id: "eleven_multilingual_v2",
                output_format: "pcm_24000",
                live_execution: true,
                detail: "Configured ElevenLabs credentials and voice are reachable.",
            };
        } else if (
            url.pathname === "/v1/integrations/elevenlabs/preview" &&
            req.method() === "POST"
        ) {
            await route.fulfill({
                status: 200,
                contentType: "audio/wav",
                headers: {
                    "X-Katcha-Estimated-Cost-USD": "0.004",
                    "X-Katcha-TTS-Provider": "elevenlabs",
                    "X-Katcha-Voice-Profile": "elevenlabs_rank_snaxx_v1",
                },
                body: Buffer.from("RIFFfixture"),
            });
            return;
        } else if (
            url.pathname === "/v1/integrations/channels/channel-1/voice-policy"
        ) {
            if (req.method() === "PUT") {
                brandVersion += 1;
                const profileByProvider = {
                    elevenlabs: "elevenlabs_rank_snaxx_v1",
                    openai: "openai_youth_v2",
                    gemini: "gemini_youth_v2",
                };
                voicePolicy = {
                    channel_profile_id: "channel-1",
                    brand_version: brandVersion,
                    routing_mode: body.routing_mode,
                    preferred_profiles: [
                        profileByProvider[body.primary_provider],
                        ...(body.fallback_providers || []).map(
                            (provider) => profileByProvider[provider],
                        ),
                    ],
                    primary_provider: body.primary_provider,
                    fallback_providers: body.fallback_providers || [],
                };
            }
            data = voicePolicy;
        } else if (url.pathname === "/v1/integrations/youtube") {
            data = [
                {
                    id: "youtube-1",
                    channel_id: "yt-channel-1",
                    channel_title: "Fixture Gaming",
                    status: "active",
                    scopes: ["youtube", "yt-analytics.readonly"],
                    token_expires_at: now,
                    last_refreshed_at: now,
                    created_at: now,
                    updated_at: now,
                },
            ];
        } else if (url.pathname === "/v1/channels/channel-1") {
            data = summary;
        } else if (
            url.pathname === "/v1/channels/channel-1/growth-goals" &&
            req.method() === "POST"
        ) {
            savedGrowthGoals = body;
            data = {
                id: "strategy-3",
                channel_profile_id: "channel-1",
                version: 3,
                ...summary.strategy,
                strategy_metadata: { growth: savedGrowthGoals },
                created_at: now,
            };
        } else if (
            url.pathname === "/v1/channels/channel-1/growth" &&
            req.method() === "GET"
        ) {
            data = {
                ...summary.growth,
                goals: savedGrowthGoals,
                ai: {
                    ...summary.growth.ai,
                    pace: savedGrowthGoals.pace,
                    selected_path:
                        savedGrowthGoals.path === "fastest"
                            ? "shorts"
                            : savedGrowthGoals.path,
                    priority_metrics: [
                        ...(savedGrowthGoals.custom_targets || []).map((goal) => goal.metric),
                        "qualified_shorts_views_90d",
                        "subscribers",
                    ],
                },
            };
        } else if (url.pathname === "/v1/publications") {
            data = publications;
        } else if (url.pathname === "/v1/productions") {
            data = [
                {
                    id: "production-1",
                    clip_id: "clip-1",
                    parent_production_id: null,
                    generation: 1,
                    regenerate_from: null,
                    workflow_id: "production-workflow-1",
                    kind: "short",
                    status: "rendering",
                    stage: "render",
                    persona_key: "host",
                    persona_version: "1",
                    prompt_version: "1",
                    edit_blueprint_key: "fast_commentary",
                    edit_blueprint_version: 2,
                    edit_blueprint_snapshot: {},
                    selected_script_id: null,
                    selected_voice_profile: null,
                    render_manifest: {},
                    estimated_cost_usd: "0.10",
                    error: null,
                    created_at: now,
                    updated_at: now,
                },
            ];
        } else if (url.pathname === "/v1/channels/channel-1/brands") {
            data = [
                {
                    id: "brand-1",
                    channel_profile_id: "channel-1",
                    version: brandVersion,
                    brand_key: "fixture_brand",
                    is_active: true,
                    contract: {
                        format: "host-led gaming",
                        voice_policy: {
                            routing_mode: voicePolicy.routing_mode,
                            preferred_profiles: voicePolicy.preferred_profiles,
                        },
                    },
                    brand_metadata: {},
                    created_at: now,
                },
            ];
        } else if (url.pathname === "/v1/publications/publication-1/analytics" && req.method() === "GET") {
            data = analytics;
        } else if (url.pathname === "/v1/publications/publication-1/analytics/refresh" && req.method() === "POST") {
            data = {
                publication_id: "publication-1",
                workflow_id: "analytics-refresh-1",
                sample_key: "manual-fixture",
            };
        } else if (url.pathname === "/v1/channels/channel-1/intelligence/refresh" && req.method() === "POST") {
            data = {
                channel_profile_id: "channel-1",
                workflow_id: "refresh-1",
                run_key: "channel-studio-fixture",
            };
        } else {
            throw new Error("Unexpected API request " + req.method() + " " + url.pathname);
        }
        await route.fulfill({ json: data });
    });

    await page.goto("http://127.0.0.1:8768/channels.html");
    assert.equal(await page.locator(".brand").getAttribute("href"), "/explorer");
    await page.locator("#token").fill("fixture-token");
    await page.locator("#connect-form button").click();
    await page.locator("#studio:not([hidden])").waitFor();
    assert.equal(await page.locator("#channel").inputValue(), "channel-1");
    assert.match(await page.locator("#channel").innerText(), /Fixture Gaming/);

    assert.equal(await page.locator("#token").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.match(await page.locator("#metric-videos").innerText(), /2/);
    assert.match(await page.locator("#metric-views").innerText(), /12\.5K|12K/);
    assert.match(await page.locator("#metric-margin").innerText(), /18\.35/);
    assert.match(await page.locator("#milestone-grid").innerText(), /Early YPP access/);
    assert.match(await page.locator("#milestone-grid").innerText(), /Ads & Premium/);
    assert.match(await page.locator("#ai-growth-focus").innerText(), /AI focus:/);
    assert.equal(await page.locator("#growth-pace").inputValue(), "aggressive");
    assert.match(await page.locator("#threshold-change").innerText(), /Feb.*1.*2027|Feb 1, 2027/);
    assert.match(await page.locator("#publication-list").innerText(), /Fixture launch breakdown/);
    assert.match(await page.locator("#video-detail").innerText(), /74\.2%/);
    assert.match(await page.locator("#packaging").innerText(), /concise curiosity-led titles/);
    assert.match(await page.locator("#editing-performance").innerText(), /fast_commentary/);
    assert.match(await page.locator("#schedule").innerText(), /Fri · 18:00/);
    assert.match(await page.locator("#brand-panel").innerText(), /fixture_brand/);
    assert.match(await page.locator("#automation-panel").innerText(), /Review Required/);
    assert.equal(await page.locator("#voice-provider").inputValue(), "openai");
    assert.equal(await page.locator("#voice-routing").inputValue(), "inherit");
    assert.match(await page.locator("#provider-panel").innerText(), /ElevenLabs TTS/);
    assert.match(await page.locator("#provider-panel").innerText(), /InVideo edit bridge/);

    await page.locator("#verify-elevenlabs").click();
    await page.getByText(/credentials and voice are reachable/).waitFor();

    await page.locator("#preview-elevenlabs").click();
    await page.locator("#voice-preview-audio:not([hidden])").waitFor();
    await page.getByText(/ElevenLabs preview ready/).waitFor();

    await page.locator("#voice-provider").selectOption("elevenlabs");
    await page.locator("#voice-routing").selectOption("fixed");
    await page.locator("#save-voice-provider").click();
    await page.getByText(/ElevenLabs is now the primary narration provider/).waitFor();
    assert.equal(await page.locator("#voice-provider-badge").innerText(), "ELEVENLABS");

    const voiceRequest = requests.find(
        (request) =>
            request.path === "/v1/integrations/channels/channel-1/voice-policy" &&
            request.method === "PUT",
    );
    assert.equal(voiceRequest.body.primary_provider, "elevenlabs");
    assert.equal(voiceRequest.body.routing_mode, "fixed");
    assert.deepEqual(voiceRequest.body.fallback_providers, ["openai", "gemini"]);

    await page.locator("#custom-goal-metric").selectOption("subscribers");
    await page.locator("#custom-goal-target").fill("5000");
    await page.locator("#custom-goal-priority").selectOption("5");
    await page.locator("#add-custom-goal").click();
    assert.match(await page.locator("#custom-goals").innerText(), /5K|5,000/);
    await page.locator("#growth-goals-form button[type=submit]").click();
    await page.getByText(/Growth goals saved as strategy v3/).waitFor();
    const goalRequest = requests.find(
        (request) =>
            request.path === "/v1/channels/channel-1/growth-goals" &&
            request.method === "POST",
    );
    assert.equal(goalRequest.body.pace, "aggressive");
    assert.equal(goalRequest.body.objective, "ads_revenue");
    assert.equal(goalRequest.body.custom_targets[0].metric, "subscribers");
    assert.equal(goalRequest.body.custom_targets[0].target, 5000);

    await page.locator("#refresh-video-analytics").click();
    await page.getByText(/Analytics refresh queued/).waitFor();
    await page.locator("#refresh-intelligence").click();
    await page.getByText(/Intelligence refresh queued/).waitFor();

    assert(
        requests.some(
            (request) =>
                request.path === "/v1/productions" &&
                request.method === "GET",
        ),
    );
    assert(
        requests.some(
            (request) =>
                request.path === "/v1/channels/channel-1/brands" &&
                request.method === "GET",
        ),
    );
    assert(
        requests.some(
            (request) =>
                request.path === "/v1/publications/publication-1/analytics/refresh" &&
                request.method === "POST",
        ),
    );
    assert(requests.every((request) => request.auth === "Bearer fixture-token"));

    await page.setViewportSize({ width: 700, height: 900 });
    assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
    );
    assert.deepEqual(errors, []);
})()
    .finally(async () => {
        if (browser) await browser.close();
        server.kill();
    })
    .catch((error) => {
        console.error(error);
        process.exitCode = 1;
    });
