"""Stream provider adapters (TASK 007).

Provider-specific logic lives HERE — outside the core analysis pipeline.
Each adapter declares honest capabilities: a provider is "implemented"
only when ``supports_materialization`` is True AND its adapter has tests.
Validation-only adapters (YouTube, Twitch) explicitly refuse to
materialize: source download requires an explicitly authorized provider
(or a user-uploaded file). No yt-dlp, no DRM bypass, no unauthorized
access — by construction and by test.
"""
