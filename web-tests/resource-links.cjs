// Offline text/identity tests. No provider content may become executable HTML.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = {window: {}, URL, URLSearchParams, location: {origin: "https://app.katcha.stream"}};
vm.runInNewContext(fs.readFileSync(require("node:path").join(__dirname, "../src/katcha/web/resource-links.js"), "utf8"), context);
const {format, resources, actions, safeHref} = context.window.KatchaResourceLinks;
const channel = "2a1dc777-fae4-4f5c-a6fd-eeabe4826af1";
const original = "aa9819a1-26d3-43c5-b3fb-64750b632673";
const meme = "caf65c87-a067-471c-8464-406e87aa0fe6";
const asset = "11111111-1111-4111-8111-111111111111";
const evidence = [original, meme].map((id, index) => ({kind: "goal_step", tool: "production", result: {
    production: {id, channel_profile_id: channel, edit_blueprint_key: index ? "meme" : "source_original"},
    assets: [{id: asset, production_id: id}],
}}));
const links = resources(evidence, channel);
assert.equal(links.size, 2, "asset identities must not become production links");
assert.match(format('Draft `' + original + '`', links), new RegExp('production=' + original));
assert.match(format('[Watch draft](' + links.get(original).href + ')'), /<a href="\/channels\?/);
assert.match(actions(original + ' and ' + meme, links), /Watch draft · Original audio/);
assert.match(actions(original + ' and ' + meme, links), /Watch draft · Meme remix/);
assert.equal(safeHref("javascript:alert(1)"), null);
assert.equal(safeHref("//evil.invalid/path"), null);
assert.equal(safeHref("/\\evil.invalid/path"), null);
assert.equal(safeHref("https://user:password@example.com"), null);
const hostile = format('<img src=x onerror=alert(1)> [bad](javascript:alert(1)) **<svg onload=x>**');
assert(!hostile.includes('<img') && !hostile.includes('<svg') && !hostile.includes('<a'));
assert.match(format('https://example.com/video.'), /rel="noopener noreferrer"/);
assert.match(format('https://example.com/video.'), /<\/a>\.$/);
assert.match(format('`/clips?clip=' + original + '`'), /<a href="\/clips\?/);
console.log("Resource links: distinct drafts, observed IDs, safe Markdown and hostile text passed");

const publication = "22222222-2222-4222-8222-222222222222";
const published = resources([{kind: "goal_step", tool: "publication", result: {id: publication}}], channel);
assert.match(published.get(publication).href, /publication=22222222/);
