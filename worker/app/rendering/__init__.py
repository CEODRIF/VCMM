"""Video rendering (FFmpeg) — Phase 5 foundation.

Fixed vertical output profile (1080x1920, H.264 + AAC, yuv420p, fast-start)
with: geometry planning (profiles), a safe subprocess wrapper (ffmpeg),
output-contract validation (validation) and the real renderer (renderer).
Animated subtitle burn-in is Phase 5b and is NOT part of this package yet.
"""
