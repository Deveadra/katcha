/* Standalone, channel-owned Clip Library intake. Reuses Editorial's existing
 * project-independent server API; never creates an Editorial Project. */
const libraryUpload = {busy: false, controller: null, sourceId: "", channel: "", fingerprint: ""};
const mediaUploadMax = 4 * 1024 ** 3;
const smallUploadMax = 512 * 1024 ** 2;
const allowedUploadExt = new Set(["mp4", "mov", "m4v", "mkv", "webm"]);
const acceptedMediaMime = new Set([
    "application/octet-stream", "video/mp4", "video/quicktime",
    "video/webm", "video/x-matroska",
]);
const safeUploadMime = file => acceptedMediaMime.has(file.type)
    ? file.type : "application/octet-stream";

function uploadMediaStatus(value, percent = null) {
    $("upload-media-status").textContent = value;
    const bar = $("upload-media-progress");
    bar.hidden = percent === null;
    if (percent !== null) bar.value = Math.max(0, Math.min(100, percent));
}

function uploadMediaOpen() {
    if (!currentChannel() || libraryUpload.busy) return;
    $("upload-media-channel").textContent = "Destination: " + channelName(currentChannel());
    uploadMediaStatus("Choose a local video to upload.");
    $("add-media-dialog").showModal();
}

function setLibraryUploadBusy(busy) {
    libraryUpload.busy = busy;
    $("upload-media-submit").disabled = busy;
    $("upload-media-close").disabled = busy;
    $("upload-media-file").disabled = busy;
    $("upload-media-title").disabled = busy;
    $("upload-media-permission").disabled = busy;
    $("channel").disabled = busy;
    $("add-media").disabled = busy || !currentChannel();
    $("upload-media-pause").hidden = !busy;
}

async function libraryUploadIdentity(file, title, channel) {
    // A small first/last fingerprint avoids confusing identically named videos.
    // R2 verifies the complete object's hash and decodes it before registration.
    const sliceBytes = 1024 * 1024;
    const first = new Uint8Array(await file.slice(0, sliceBytes).arrayBuffer());
    const last = new Uint8Array(await file.slice(Math.max(0, file.size - sliceBytes)).arrayBuffer());
    const descriptor = JSON.stringify({
        channel, filename: file.name, size: file.size,
        lastModified: file.lastModified, title,
    });
    const digest = async bytes => [...new Uint8Array(
        await crypto.subtle.digest("SHA-256", bytes),
    )].map(x => x.toString(16).padStart(2, "0")).join("");
    const key = "katcha.clip-library.upload." + await digest(
        new TextEncoder().encode(JSON.stringify({
            descriptor,
            first: await digest(first),
            last: await digest(last),
        })),
    );
    libraryUpload.fingerprint = key;
    try {
        const existing = localStorage.getItem(key);
        if (existing) return existing;
        const id = crypto.randomUUID();
        localStorage.setItem(key, id);
        return id;
    } catch {
        // Storage-restricted browsers can still resume within this tab.
        const existing = sessionStorage.getItem(key);
        if (existing) return existing;
        const id = crypto.randomUUID();
        sessionStorage.setItem(key, id);
        return id;
    }
}

async function uploadLibraryMultipart(file, title, idempotency, channel, signal) {
    const base = "/v1/channels/" + encodeURIComponent(channel) +
        "/editorial-projects/source-uploads/multipart";
    const begin = await api(base, {
        method: "POST", signal,
        body: JSON.stringify({
            filename: file.name, title: title || null, size_bytes: file.size,
            content_type: safeUploadMime(file),
            permitted_use: true, idempotency_key: idempotency,
        }),
    });
    libraryUpload.sourceId = begin.source_id;
    if (begin.state === "ready") return begin;
    if (begin.state === "verifying") {
        uploadMediaStatus("Completing verification of previously uploaded media…", 100);
        return api(base + "/" + encodeURIComponent(begin.source_id) + "/complete", {method: "POST", signal});
    }
    if (begin.state !== "uploading") {
        throw new Error("This upload is not resumable. Discard the unfinished upload before retrying.");
    }
    const url = base + "/" + encodeURIComponent(begin.source_id);
    const detail = await api(url, {signal});
    if (detail.state === "ready") return detail;
    if (detail.state === "verifying") {
        uploadMediaStatus("Verifying previously uploaded media…", 100);
        return api(url + "/complete", {method: "POST", signal});
    }
    if (detail.state !== "uploading") {
        throw new Error("This upload cannot resume in its current state.");
    }
    const done = new Set((detail.parts || []).map(part => part.part_number));
    let completed = (detail.parts || []).reduce((sum, part) => sum + part.size, 0);
    let next = 1;
    let stopped = false;
    const update = () => uploadMediaStatus(
        "Uploading " + file.name + ": " +
        Math.floor(Math.min(100, completed * 100 / file.size)) + "% (" +
        formatBytes(completed) + " of " + formatBytes(file.size) + ")",
        completed * 100 / file.size,
    );
    update();
    async function worker() {
        while (!stopped && next <= begin.part_count) {
            if (signal.aborted) throw new DOMException("Upload paused", "AbortError");
            const partNumber = next++;
            if (done.has(partNumber)) continue;
            const offset = (partNumber - 1) * begin.part_bytes;
            const chunk = file.slice(offset, Math.min(file.size, offset + begin.part_bytes));
            let accepted = false;
            for (let attempt = 0; attempt < 3 && !accepted; attempt++) {
                if (signal.aborted) throw new DOMException("Upload paused", "AbortError");
                if (stopped) return;
                const signed = await api(url + "/parts", {
                    method: "POST", signal, body: JSON.stringify({part_number: partNumber}),
                });
                let response;
                try {
                    // Presigned R2 URL, with no control bearer token or cookies.
                    response = await fetch(signed.url, {
                        method: "PUT", body: chunk, signal, credentials: "omit",
                    });
                } catch (error) {
                    if (signal.aborted) throw error;
                    if (attempt === 2) {
                        throw new Error("R2 transfer failed. Check network and R2 allowed-origin PUT CORS. Reselect this file to resume.");
                    }
                    continue;
                }
                if (response.ok) accepted = true;
                else if (attempt === 2) {
                    throw new Error("R2 rejected part " + partNumber +
                        " (HTTP " + response.status + "). Reselect the same file to resume.");
                }
            }
            if (!accepted) throw new Error("Could not transfer part " + partNumber);
            completed += chunk.size;
            update();
        }
    }
    // Only two concurrent chunks; avoids buffering 4 GiB in the browser or OCI.
    // An unrecoverable failure in one transfer must not let the other drain
    // the entire multi-GiB upload while the UI is already failing.
    async function boundedWorker() {
        try { await worker(); }
        catch (error) { stopped = true; throw error; }
    }
    const results = await Promise.allSettled([boundedWorker(), boundedWorker()]);
    const failed = results.find(x => x.status === "rejected");
    if (failed) throw failed.reason;
    uploadMediaStatus("Transfer complete. Verifying the full video and registering the clip…", 100);
    return api(url + "/complete", {method: "POST", signal});
}

async function uploadLibraryMedia() {
    if (libraryUpload.busy) return;
    const channel = currentChannel()?.id;
    const file = $("upload-media-file").files[0];
    const title = $("upload-media-title").value.trim();
    if (!channel) return uploadMediaStatus("Choose a specific channel first.");
    if (!file) return uploadMediaStatus("Choose a video file.");
    if (!allowedUploadExt.has(file.name.split(".").pop().toLowerCase())) {
        return uploadMediaStatus("Choose MP4, MOV, M4V, MKV or WebM video.");
    }
    if (!(file.size > 0 && file.size <= mediaUploadMax)) {
        return uploadMediaStatus("Video must be between 1 byte and 4 GiB.");
    }
    if (!$("upload-media-permission").checked) {
        return uploadMediaStatus("Confirm you have permission to use this video.");
    }
    libraryUpload.sourceId = "";
    libraryUpload.channel = channel;
    libraryUpload.controller = new AbortController();
    setLibraryUploadBusy(true);
    $("upload-media-discard").hidden = true;
    uploadMediaStatus("Preparing " + file.name + "…", 0);
    try {
        const idempotency = await libraryUploadIdentity(file, title, channel);
        if (libraryUpload.controller.signal.aborted) return;
        let result;
        if (file.size > smallUploadMax) {
            result = await uploadLibraryMultipart(
                file, title, idempotency, channel, libraryUpload.controller.signal,
            );
        } else {
            const params = new URLSearchParams({
                filename: file.name,
                idempotency_key: idempotency,
                permitted_use: "true",
            });
            if (title) params.set("title", title);
            uploadMediaStatus("Uploading " + file.name + " to Katcha…", 0);
            // Small uploads use the existing bounded server-side route.
            const response = await request(
                "/v1/channels/" + encodeURIComponent(channel) +
                    "/editorial-projects/source-uploads?" + params,
                {
                    method: "POST", body: file, signal: libraryUpload.controller.signal,
                    headers: {"Content-Type": safeUploadMime(file)},
                },
            );
            if (!response.ok) {
                let detail = "";
                try { detail = (await response.json()).detail || ""; } catch {}
                throw new Error(typeof detail === "string" && detail
                    ? detail : "Upload failed (HTTP " + response.status + ").");
            }
            result = await response.json();
        }
        if (!result.clip_id) throw new Error("Upload did not return a managed clip. Inspect the server receipt before retrying.");
        uploadMediaStatus(
            "Added " + (result.title || file.name) + " to Clip Library (" +
            result.clip_id + ")." + (result.deduplicated ? " Existing media reused." : ""),
            100,
        );
        libraryUpload.sourceId = "";
        // Show the new clip even if an earlier search/filter hid it.
        state.bucket = "all";
        state.linkedClip = "";
        $("search").value = "";
        $("status-filter").value = "all";
        await refreshLibrary();
        if (state.clips.some(clip => clip.id === result.clip_id)) {
            await selectClip(result.clip_id);
        }
        message("Source video added to this channel's Clip Library. No project was created.");
    } catch (error) {
        uploadMediaStatus(
            libraryUpload.controller.signal.aborted
                ? "Transfer paused. Reselect the same video and title to resume completed parts."
                : error.message + " Reselect the same video and title to retry.",
        );
        $("upload-media-discard").hidden = !libraryUpload.sourceId;
    } finally {
        libraryUpload.controller = null;
        setLibraryUploadBusy(false);
    }
}

$("add-media").addEventListener("click", uploadMediaOpen);
$("upload-media-submit").addEventListener("click", () => void uploadLibraryMedia());
$("upload-media-close").addEventListener("click", () => {
    if (!libraryUpload.busy) $("add-media-dialog").close();
});
$("upload-media-pause").addEventListener("click", () => libraryUpload.controller?.abort());
$("add-media-dialog").addEventListener("cancel", event => {
    if (libraryUpload.busy) event.preventDefault();
});
$("upload-media-discard").addEventListener("click", async () => {
    if (libraryUpload.busy || !libraryUpload.sourceId) return;
    if (!confirm("Discard this unfinished R2 upload? Completed parts will be lost.")) return;
    const button = $("upload-media-discard");
    button.disabled = true;
    try {
        await api(
            "/v1/channels/" + encodeURIComponent(libraryUpload.channel) +
                "/editorial-projects/source-uploads/multipart/" +
                encodeURIComponent(libraryUpload.sourceId),
            {method: "DELETE"},
        );
        libraryUpload.sourceId = "";
        if (libraryUpload.fingerprint) {
            try { localStorage.removeItem(libraryUpload.fingerprint); } catch {}
            try { sessionStorage.removeItem(libraryUpload.fingerprint); } catch {}
        }
        button.hidden = true;
        uploadMediaStatus("Unfinished transfer discarded. Choose a file to start again.");
    } catch (error) {
        uploadMediaStatus("Could not discard upload: " + error.message);
    } finally {
        button.disabled = false;
    }
});
