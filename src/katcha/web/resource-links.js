/* Safe links for native resources and AI text. Provider text is never HTML. */
(() => {
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const escape = (value) => String(value ?? "").replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;").replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    function safeHref(value) {
        if (typeof value !== "string" || /[\u0000-\u0020\\]/.test(value)) return null;
        if (!value.startsWith("/") && !/^https?:\/\//i.test(value)) return null;
        if (value.startsWith("//")) return null;
        try {
            const url = new URL(value, location.origin);
            if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
            return value;
        } catch { return null; }
    }
    function anchor(label, href) {
        const safe = safeHref(href);
        if (!safe) return escape(label);
        const external = new URL(safe, location.origin).origin !== location.origin;
        return '<a href="' + escape(safe) + '"' +
            (external ? ' target="_blank" rel="noopener noreferrer"' : '') + '>' + escape(label) + '</a>';
    }
    function resources(evidence, channelId) {
        const found = new Map();
        function add(kind, id, channel, title) {
            if (!uuid.test(String(id || "")) || !uuid.test(String(channel || ""))) return;
            const params = new URLSearchParams({channel});
            let label, route;
            if (kind === "production") {
                params.set("production", id); route = "/channels"; label = "Watch draft";
            } else if (kind === "clip") {
                params.set("clip", id); route = "/clips"; label = "Open source clip";
            } else if (kind === "publication") {
                params.set("publication", id); route = "/channels"; label = "Open video";
            } else return;
            if (found.has(id.toLowerCase()) && !title) return;
            found.set(id.toLowerCase(), {id, label: title ? label + " · " + title : label, href: route + "?" + params +
                (kind === "clip" ? "" : "#content")});
        }
        function visit(value, hint, inheritedChannel, depth = 0) {
            if (!value || typeof value !== "object" || depth > 12) return;
            if (Array.isArray(value)) {
                value.forEach((item) => visit(item, hint, inheritedChannel, depth + 1)); return;
            }
            const channel = value.channel_profile_id || inheritedChannel;
            const kind = ["production", "clip", "publication"].includes(value.kind) ? value.kind : hint;
            const title = value.title || (kind === "production" ? ({
                source_original: "Original audio", meme: "Meme remix", persona_commentary: "Commentary",
            })[value.edit_blueprint_key] : null);
            if (value.id && kind) add(kind, value.id, channel, title);
            if (value.production_id) add("production", value.production_id, channel);
            if (value.clip_id) add("clip", value.clip_id, channel);
            if (value.publication_id) add("publication", value.publication_id, channel);
            const resultHint = {production: "production", productions: "production",
                clip_library: "clip", find_clips: "clip", publication: "publication"}[value.tool];
            for (const [key, child] of Object.entries(value)) {
                const next = ["production", "clip", "publication"].includes(key) ? key
                    : key === "result" && resultHint ? resultHint : key === "items" ? hint : null;
                visit(child, next, channel, depth + 1);
            }
        }
        visit(evidence, null, channelId);
        return found;
    }
    function format(text, links = new Map()) {
        const input = String(text || "");
        // Tokenize first and escape each text token. Never inject provider HTML.
        const pattern = /\[([^\]\n]+)\]\(([^\s)]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|https?:\/\/[^\s<>"']+|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
        let html = "", offset = 0;
        for (const match of input.matchAll(pattern)) {
            html += escape(input.slice(offset, match.index));
            if (match[1] !== undefined) {
                html += safeHref(match[2]) ? anchor(match[1], match[2]) : escape(match[0]);
            } else if (match[3] !== undefined) {
                const link = links.get(match[3].toLowerCase());
                html += '<code>' + (link ? anchor(match[3], link.href)
                    : safeHref(match[3]) ? anchor(match[3], match[3]) : escape(match[3])) + '</code>';
            } else if (match[4] !== undefined) html += '<strong>' + escape(match[4]) + '</strong>';
            else if (/^https?:/i.test(match[0])) {
                const href = match[0].replace(/[.,;!?)]+$/, "");
                html += anchor(href, href) + escape(match[0].slice(href.length));
            } else {
                const link = links.get(match[0].toLowerCase());
                html += link ? anchor(match[0], link.href) : escape(match[0]);
            }
            offset = match.index + match[0].length;
        }
        return html + escape(input.slice(offset));
    }
    function actions(text, links) {
        const values = [...links.values()];
        const referenced = values.filter((link) => String(text).toLowerCase().includes(link.id.toLowerCase()));
        const selected = referenced.length ? referenced : values;
        if (!selected.length) return "";
        return '<nav class="message-resource-links" aria-label="Referenced media">' +
            selected.slice(0, 12).map((link) => anchor(link.label, link.href)).join("") +
            '</nav>';
    }
    window.KatchaResourceLinks = {safeHref, resources, format, actions};
})();
