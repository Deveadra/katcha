// Contract test for the actual Clip Library multipart client, using virtual 3 GiB
// media and an in-memory R2 inventory. No fixtures, credentials, or live costs.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CH = "11111111-1111-4111-8111-111111111111";
const PART = 64 * 1024 ** 2;
const SIZE = 3 * 1024 ** 3;
const SOURCE = "22222222-2222-4222-8222-222222222222";
const base = "/v1/channels/" + CH + "/editorial-projects/source-uploads/multipart";
const bars = new Map();
const $ = name => bars.get(name) || (bars.set(name, {
    hidden: false, value: 0, textContent: "", addEventListener: () => {},
}), bars.get(name));
const received = new Set();
const sent = [];
let failPart = null, complete = 0, callCount = 0;
const file = {
    name: "large.mp4", type: "video/mp4", size: SIZE, lastModified: 1,
    slice(start, end) {
        assert(start >= 0 && end <= SIZE && start < end);
        // Only size is used: no test ever materializes 64 MiB in memory.
        return {size: end - start};
    },
};
const receipt = () => ({
    state: "uploading", source_id: SOURCE, part_bytes: PART, part_count: 48,
});
const sandbox = {
    console, Set, Promise, TextEncoder, URLSearchParams, DOMException,
    encodeURIComponent, $,
    formatBytes: bytes => String(bytes),
    api: async (url, opts = {}) => {
        assert(url.startsWith(base), "No project or proxied upload path is allowed");
        callCount++;
        if (url === base && opts.method === "POST") {
            const request = JSON.parse(opts.body);
            assert.equal(request.content_type, "video/mp4");
            assert.equal(request.size_bytes, SIZE);
            assert.equal(request.permitted_use, true);
            return receipt();
        }
        if (url.endsWith("/parts")) {
            const number = JSON.parse(opts.body).part_number;
            assert(number >= 1 && number <= 48);
            return {url: "https://r2.invalid/part/" + number};
        }
        if (url.endsWith("/complete")) {
            complete++;
            assert.equal(received.size, 48, "Cannot complete before all parts are received");
            return {...receipt(), state: "ready", clip_id: "33333333-3333-4333-8333-333333333333"};
        }
        if (!opts.method || opts.method === "GET") {
            return {...receipt(), parts: [...received].sort((a,b) => a-b).map(part_number => ({
                part_number, size: PART,
            }))};
        }
        throw new Error("Unexpected route " + url);
    },
    fetch: async (url, opts = {}) => {
        assert.equal(opts.method, "PUT");
        assert.equal(opts.credentials, "omit", "Never send cookies to signed R2 URLs");
        const part = Number(url.split("/").pop());
        assert.equal(opts.body.size, PART);
        sent.push(part);
        if (part === failPart) throw new Error("fixture offline");
        received.add(part);
        return {ok: true, status: 200};
    },
};
const context = vm.createContext(sandbox);
vm.runInContext(
    readFileSync(path.join(__dirname, "../src/katcha/web/clips-upload.js"), "utf8"),
    context, {filename: "clips-upload.js"},
);
const multipart = vm.runInContext("uploadLibraryMultipart", context);
const normalizeMime = vm.runInContext("safeUploadMime", context);

(async () => {
    assert.equal(normalizeMime({type: "video/x-m4v"}), "application/octet-stream");
    assert.equal(normalizeMime({type: "video/mp4"}), "video/mp4");

    // R2 succeeds for part 1/2, then part 3 repeatedly fails.
    failPart = 3;
    await assert.rejects(
        () => multipart(file, "Trailer", "stable-identity", CH, new AbortController().signal),
        /R2 transfer failed/,
    );
    assert.equal(complete, 0);
    const sentAfterFailure = sent.length;
    assert(sentAfterFailure < 20,
        "Sibling worker continued uploading too many parts after terminal R2 failure");
    const retained = received.size;
    assert(retained >= 1 && retained < 48);

    failPart = null;
    const done = await multipart(file, "Trailer", "stable-identity", CH, new AbortController().signal);
    assert.equal(done.state, "ready");
    assert.equal(received.size, 48);
    assert.equal(complete, 1);
    for (const part of received) assert.equal(
        sent.filter(number => number === part).length, part === 3 ? 4 : 1,
        "Resumption must not transmit a server-confirmed R2 part twice",
    );
    assert(callCount > 50);
    console.log("Clip Library R2: virtual 3 GiB, fail-fast sibling, resume and MIME fallback passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
