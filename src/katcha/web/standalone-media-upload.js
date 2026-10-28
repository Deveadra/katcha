/* Project-free uploads to the channel Clip Library via the existing R2 multipart API.
 * Kept separate from the Content receipt uploader so small-file and URL flows
 * retain their existing behavior. No new storage endpoint or rights bypass.
 */
window.KatchaStandaloneMediaUpload = (() => {
    const LARGE_THRESHOLD = 512 * 1024 ** 2;
    const MAX_BYTES = 4 * 1024 ** 3;
    const SAMPLE_BYTES = 1024 * 1024;
    const allowedTypes = new Set([
        "application/octet-stream", "video/mp4", "video/quicktime",
        "video/webm", "video/x-matroska",
    ]);

    async function sha256(bytes) {
        const hash = await crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
    }

    async function requestIdentity(file, title, channel) {
        // Bounded reads, even for multi-GiB footage. Server independently
        // hashes the full R2 object before assigning a canonical Clip ID.
        const [front, back] = await Promise.all([
            file.slice(0, SAMPLE_BYTES).arrayBuffer(),
            file.slice(Math.max(0, file.size - SAMPLE_BYTES)).arrayBuffer(),
        ]);
        const key = JSON.stringify({
            channel, title, filename: file.name, size: file.size,
            modified: file.lastModified, first: await sha256(front),
            last: await sha256(back),
        });
        return "standalone-" + await sha256(new TextEncoder().encode(key));
    }

    async function upload({file, title, channel, api, onProgress, shouldPause}) {
        if (!file || file.size <= LARGE_THRESHOLD || file.size > MAX_BYTES) {
            throw new Error("Direct R2 upload requires a video over 512 MiB and no larger than 4 GiB.");
        }
        if (!channel) throw new Error("Choose a channel for this media.");
        const type = allowedTypes.has(file.type) ? file.type : "application/octet-stream";
        const base = "/v1/channels/" + encodeURIComponent(channel) +
            "/editorial-projects/source-uploads/multipart";
        const key = await requestIdentity(file, title, channel);
        const started = await api(base, {
            method: "POST",
            body: JSON.stringify({
                filename: file.name, title, size_bytes: file.size, content_type: type,
                permitted_use: true, idempotency_key: key,
            }),
        });
        const itemPath = base + "/" + encodeURIComponent(started.source_id);
        if (started.state === "ready") return started;
        if (started.state === "verifying") {
            onProgress({phase: "verifying", received: file.size, size: file.size});
            return api(itemPath + "/complete", {method: "POST"});
        }
        if (started.state !== "uploading") {
            throw new Error("The saved upload is not accepting parts. Inspect its receipt before retrying.");
        }
        // The server queries the actual R2 part inventory. A lost response
        // must not make the browser transmit acknowledged parts again.
        const snapshot = await api(itemPath);
        if (snapshot.state === "ready") return snapshot;
        if (snapshot.state === "verifying") {
            onProgress({phase: "verifying", received: file.size, size: file.size});
            return api(itemPath + "/complete", {method: "POST"});
        }
        if (snapshot.state !== "uploading") {
            throw new Error("The saved upload changed state. Inspect it before retrying.");
        }
        const receivedParts = new Set((snapshot.parts || []).map(p => p.part_number));
        let received = (snapshot.parts || []).reduce((sum, part) => sum + part.size, 0);
        let nextPart = 1, stopped = false;
        const progress = () => onProgress({phase: "uploading", received, size: file.size});
        progress();

        async function worker() {
            while (!stopped && !shouldPause() && nextPart <= started.part_count) {
                const partNumber = nextPart++;
                if (receivedParts.has(partNumber)) continue;
                const offset = (partNumber - 1) * started.part_bytes;
                const blob = file.slice(offset, Math.min(file.size, offset + started.part_bytes));
                let saved = false;
                for (let attempt = 0; attempt < 3 && !saved; attempt++) {
                    const signature = await api(itemPath + "/parts", {
                        method: "POST", body: JSON.stringify({part_number: partNumber}),
                    });
                    let response;
                    try {
                        // Only scoped, expiring signed URLs reach the browser.
                        // Content bytes go browser -> R2, not through OCI.
                        response = await fetch(signature.url, {method: "PUT", body: blob});
                    } catch {
                        if (attempt === 2) {
                            throw new Error("R2 connection or CORS failed for part " + partNumber +
                                ". Saved parts remain resumable.");
                        }
                        continue;
                    }
                    if (response.ok) saved = true;
                    else if (attempt === 2) {
                        throw new Error("R2 rejected part " + partNumber +
                            " (HTTP " + response.status + "). Saved parts remain resumable.");
                    }
                }
                if (!saved) throw new Error("Upload part " + partNumber + " was not accepted.");
                received += blob.size;
                progress();
            }
        }
        const results = await Promise.allSettled([
            worker().catch(error => { stopped = true; throw error; }),
            worker().catch(error => { stopped = true; throw error; }),
        ]);
        const failed = results.find(result => result.status === "rejected");
        if (failed) throw failed.reason;
        if (shouldPause()) return {paused: true, source_id: started.source_id};
        onProgress({phase: "verifying", received: file.size, size: file.size});
        // The server validates part order/size, then hashes and probes the
        // entire object before publishing the Clip in the library.
        return api(itemPath + "/complete", {method: "POST"});
    }
    return {upload, LARGE_THRESHOLD, MAX_BYTES};
})();
