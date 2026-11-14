"""Stream provider adapters (TASK 007, materialization per TASK 014).

Provider-specific logic lives HERE — outside the core analysis pipeline.
Each adapter declares honest capabilities: a provider is "implemented"
only when ``supports_materialization`` is True AND its adapter has tests.

Materialization truth (TASK 014 — per URL class, never per brand):
- upload: the user-authorized file (private bucket) — always materializes;
- direct_url: public video-file URLs, SSRF-guarded streaming import;
- twitch: CLIP URLs via the official Get Clips Download endpoint under
  the creator's OAuth authorization; VOD/live URLs have NO official
  media endpoint and are refused per-URL-class (metadata stays intact);
- youtube: validation only — the official APIs expose no media-download
  endpoint, so materialization is refused, never worked around.

No yt-dlp, no DRM bypass, no cookie/session theft, no unauthorized
access — by construction and by test.
"""
