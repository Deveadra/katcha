# Katcha Opportunity Activation

Opportunity activation closes the deterministic gap between a qualified trend opportunity and the existing ranked ShortEpisode planner.

## Boundary

Activation does **not** browse, download, run AI, generate narration, render, publish, or decide whether public media is legally reusable.

It may use only media already represented by the opportunity's frozen evidence packet and only when the referenced discovery candidate has:

1. been promoted into the existing SourceItem pipeline;
2. completed ingest to a Clip;
3. completed ClipFeature scoring; and
4. retained a current production-eligible acquisition assessment.

The normal ShortEpisode planner rechecks clip production eligibility when the plan is persisted. Editorial AI, review, publication, budget and channel automation gates remain unchanged.

## Preview

`GET /v1/channels/{channel_profile_id}/trends/opportunities/{opportunity_id}/activation`

The preview validates the opportunity through the same frozen trend-context contract used by editorial. Expired, below-threshold, foreign-channel, stale-watch, or evidence-less opportunities fail closed.

For every evidence media reference containing a `discovery_candidate_id`, Katcha resolves:

`DiscoveryCandidate -> SourceItem -> Clip -> ClipFeature`

Excluded references receive an explicit reason such as:

- `discovery_candidate_missing`
- `candidate_not_promoted`
- `source_item_missing`
- `source_not_ingested`
- `clip_not_scored`
- `clip_not_production_eligible`
- `duplicate_clip`

Distinct candidates that resolve to the same canonical clip are collapsed before ranking.

## Zero-token ranking signal derivation

`trend-opportunity-activation-v1` maps already-stored ClipFeature evidence into the existing ShortEpisodeCandidateInput dimensions. AI-derived feature values are used only when they already exist from prior clip analysis; activation itself makes no AI call. Missing dimensions fall back deterministically to the stored candidate score.

The derived dimensions feed the existing channel brand's ranked-short format and `build_ranking_episode_plan`. The active format still controls allowed/default item count and the existing ranking algorithm decides opener/build/false-peak/payoff placement.

## Activation

`POST /v1/channels/{channel_profile_id}/trends/opportunities/{opportunity_id}/activate`

Activation uses a deterministic key derived from:

- channel profile;
- TrendOpportunity;
- evidence packet hash;
- format key/version; and
- item count.

Repeated activation of the same frozen opportunity/plan shape therefore resolves to the same ShortEpisode planning workflow identity.

The episode snapshot freezes:

- trend opportunity and evidence packet ID/hash/version through `trend_context`;
- activation algorithm and activation key;
- every eligible discovery candidate, SourceItem and Clip ID;
- deterministic ranking signals and derivation metadata;
- exclusions/reason codes; and
- the previewed ordered clip IDs.

ShortEpisodeItem rows continue to freeze per-clip analysis, acquisition and source snapshots.

## Next phase

P7.2 may scan and activate opportunities automatically, but it must reuse this exact resolver and the existing channel AutomationLevel/economics controls. Automatic planning must never relax acquisition eligibility, source-health, AI-budget, human-review or publication gates.
