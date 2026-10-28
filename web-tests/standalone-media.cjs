// Deterministic contract test: no huge fixtures, no live R2, no project creation.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const {webcrypto} = require("node:crypto");

const PART = 64 * 1024 ** 2;
const TOTAL = 3 * 1024 ** 3;
const channel = "11111111-1111-4111-8111-111111111111";
const base = "/v1/channels/" + channel + "/editorial-projects/source-uploads/multipart";
const file = {
    name: "original-trailer.mp4", type: "video/mp4", size: TOTAL, lastModified: 1730000000,
    slice(start, end) {
        // Virtual 3 GiB file: prove bounded sample sizes and 64 MiB part
        // boundaries without allocating movie-size test buffers.
        assert(start >= 0 && end <= TOTAL && end >= start);
        return {
            size: end - start,
            arrayBuffer: async () => {
                assert(end - start <= 1024 * 1024, "Only bounded fingerprint samples may be read into JS memory");
                return Uint8Array.from([start % 251, end % 251]).buffer;
            },
        };
    },
};
const parts = new Set();
const seenKeys = [];
const sent = [];
let state = "uploading", failPart = null, completes = 0;
const response = () => ({
    source_id: "22222222-2222-4222-8222-222222222222",
    clip_id: state === "ready" ? "33333333-3333-4333-8333-333333333333" : null,
    state, part_bytes: PART, part_count: 48, filename: file.name,
});
const api = async (url, options = {}) => {
    assert(url.startsWith(base), "Upload must never invoke a project or Content byte proxy endpoint");
    const method = options.method || "GET";
    if (url === base && method === "POST") {
        const body = JSON.parse(options.body);
        assert.equal(body.permitted_use, true);
        assert.equal(body.size_bytes, TOTAL);
        seenKeys.push(body.idempotency_key);
        return response();
    }
    if (url.endsWith("/complete")) {
        completes++;
        assert.equal(parts.size, 48, "Server may not be asked to complete missing parts");
        state = "ready";
        return response();
    }
    if (url.endsWith("/parts")) {
        assert.equal(method, "POST");
        const number = JSON.parse(options.body).part_number;
        assert(number >= 1 && number <= 48);
        return {url: "https://r2.fixture/part/" + number};
    }
    if (method === "GET") {
        return {...response(), parts: [...parts].sort((a,b) => a-b).map(number => ({
            part_number: number, size: PART,
        }))};
    }
    throw new Error("Unexpected API call " + method + " " + url);
};
const sandbox = {
    window: {}, crypto: webcrypto, TextEncoder, Uint8Array, Set, Promise, encodeURIComponent,
    fetch: async (url, {method, body}) => {
        assert.equal(method, "PUT");
        const number = Number(url.split("/").at(-1));
        assert.equal(body.size, PART);
        sent.push(number);
        if (number === failPart) throw new Error("fixture R2 offline");
        parts.add(number);
        return {ok: true, status: 200};
    },
};
vm.runInNewContext(
    fs.readFileSync(path.resolve(__dirname, "../src/katcha/web/standalone-media-upload.js"), "utf8"),
    sandbox,
    {filename: "standalone-media-upload.js"},
);
const upload = sandbox.window.KatchaStandaloneMediaUpload.upload;
const args = {file, title: "Original trailer", channel, api};

(async () => {
    let pause = false;
    const first = await upload({
        ...args, shouldPause: () => pause,
        onProgress: ({phase, received}) => { if (phase === "uploading" && received >= 2 * PART) pause = true; },
    });
    assert.equal(first.paused, true);
    assert(parts.size >= 2 && parts.size < 48, "Pause must preserve partial R2 progress");

    failPart = 5;
    await assert.rejects(
        () => upload({...args, shouldPause: () => false, onProgress: () => {}}),
        /R2 connection or CORS failed for part 5/,
    );
    assert.equal(completes, 0, "Failed transfers must never claim completion");
    assert(!parts.has(5));

    failPart = null;
    const resumed = await upload({...args, shouldPause: () => false, onProgress: () => {}});
    assert.equal(resumed.state, "ready");
    assert.equal(resumed.clip_id, "33333333-3333-4333-8333-333333333333");
    assert.equal(parts.size, 48);
    assert.equal(new Set(seenKeys).size, 1, "Repeated selection must keep the same request ID");
    assert.equal(completes, 1);
    assert(sent.length < 48 + 3 + 48, "Resumption should not retransmit all previously saved parts");

    const before = sent.length;
    await upload({...args, shouldPause: () => false, onProgress: () => {}});
    assert.equal(sent.length, before, "A completed upload must replay without new R2 PUTs");
    assert.equal(completes, 1, "A completed upload must not be finalized twice");
    console.log("Standalone media: virtual 3 GiB, pause, R2 denial, resume, dedupe and no project passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
