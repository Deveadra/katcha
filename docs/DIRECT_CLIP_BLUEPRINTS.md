# Direct clips and meme edits

“Get this clip and prepare it” selects `source_original` unless the request asks
for a different treatment. It retains source audio, adds no generated commentary,
and prepares a draft from measured downloaded media without waiting for an AI
candidate score. The built-in source treatment supports up to 180 seconds; a
125-second trailer stays 125 seconds. Existing rendered snapshots are preserved.

`persona_commentary` remains available when commentary is requested.
`header_explainer` adds a text explanation. `meme` uses the source-only treatment
and searches for contextual soundbits, reaction images, and short reaction videos.
It downloads and normalizes usable assets, records their URLs and content hashes,
and adds brief timed accents. The result reports an error if no asset can be
prepared. Meme edits do not promise a particular reach or viral outcome.

Saved source usage preferences control automatic research. They do not veto an
explicit download or the Add to Clips action. Downloading and drafting retain the
observed rights state; they do not manufacture clearance or authorize publication.

Meme searches use the configured subscription research connection with Gemini
as an additional grounded search path. Gemini results must have provider grounding
sources, use existing budget checks, and record token and search-query costs. A
saved search receipt prevents replay after an uncertain failure; regenerate the
production for another attempt. All media goes through the existing object store.

Synthetic validation includes discovery/download/storage/manifest fixtures and a
local render with a visible reaction image and audible soundbit. These tests do
not establish live provider availability, particular third-party asset availability,
or production deployment.
