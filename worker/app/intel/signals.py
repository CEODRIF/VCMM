"""Signal event model (TASK 007).

A ``SignalEvent`` is ONE measured observation on the absolute source
timeline (never an estimate about content that was not measured). Every
event carries the evidence it was derived from, so fusion/selection scores
stay explainable and auditable, and nothing downstream can fabricate a
signal that no detector emitted.

``event_key`` is a stable content hash — the same signal detected twice
(same chunk re-run, same detection) yields the same key, which is what
makes the ``candidate_events`` table exact-deduplicating on retry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

#: The signal families the foundation measures. facecam (TASK 008) is a
#: real detector (MediaPipe Tasks, CPU); game vision (TASK 009) is local
#: OCR (tesseract) of kill-feed/scoreboard regions — both are OPTIONAL
#: modalities: an unimplemented/disabled family simply never contributes
#: evidence.
SIGNAL_FAMILIES = ("audio", "visual", "facecam", "game_vision")

# audio kinds
KIND_ENERGY_SPIKE = "energy_spike"          # sudden vocal-energy rise
KIND_TRANSIENT = "transient"                # short high-peak burst (explosion-like)
KIND_VOICE_ACTIVITY_CHANGE = "voice_activity_change"  # sustained regime shift (heuristic)
# visual kinds
KIND_SCENE_CHANGE = "scene_change"          # measured cut (ffmpeg scene score)
KIND_MOTION_BURST = "motion_burst"          # frame-difference surge
# facecam kinds (TASK 008) — measured facial geometry/motion patterns.
# NOTE: these are observable visual patterns, NEVER emotion/state claims;
# every event carries the measured values + detector metadata in evidence.
KIND_FACE_MOUTH_BURST = "face_mouth_burst"          # sudden mouth-opening ratio rise
KIND_FACE_SMILE_GEOMETRY = "face_smile_geometry"    # sustained smile-like geometry
KIND_FACE_HEAD_MOTION = "face_head_motion"          # sudden head rotation delta
KIND_FACE_BROW_ACTIVITY = "face_brow_activity"      # brow/upper-face lift vs median
# game-vision kinds (TASK 009) — OCR-observed text/events. The OBSERVED
# text/values are measured; the higher-level interpretation (kill,
# result...) is heuristic and flagged as such in each event's evidence.
KIND_GF_KILL_FEED_ENTRY = "kill_feed_entry"            # new transient text in kill-feed region
KIND_GF_SCOREBOARD_APPEARANCE = "scoreboard_appearance"  # scoreboard likely visible (heuristic)
KIND_GF_SCORE_CHANGE = "score_change"                  # OCR-observed value changed
KIND_GF_MATCH_RESULT_TEXT = "match_result_text"        # generic result text observed (heuristic read)


@dataclass(frozen=True)
class SignalEvent:
    """One measured signal at absolute source time ``t`` (seconds)."""

    t: float
    t_end: float
    family: str                       # "audio" | "visual"
    kind: str                         # one of the KIND_* constants
    strength: float                   # 0..1, relative (detector-defined)
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family not in SIGNAL_FAMILIES:
            raise ValueError(f"unknown signal family: {self.family!r}")
        if not self.t >= 0 or not self.t_end >= self.t:
            raise ValueError("signal times must satisfy 0 <= t <= t_end")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength must be within [0, 1]")

    @property
    def key_seed(self) -> str:
        # Bucket the start time to 0.1 s so a re-detection of the same
        # physical signal (slightly different window edge) hashes equal.
        return f"{round(self.t * 10) / 10:.1f}|{self.family}|{self.kind}"

    def to_row(self, job_id: str) -> dict[str, Any]:
        """Row for the ``candidate_events`` table (score/confidence default
        to 0 — fusion fills them in a later update pass)."""
        return {
            "job_id": job_id,
            "event_key": event_key(job_id, self),
            "start_time": round(self.t, 3),
            "end_time": round(self.t_end, 3),
            "category": "unclassified",
            "score": 0,
            "confidence": 0,
            "evidence": self.evidence,
        }


def event_key(job_id: str, event: SignalEvent) -> str:
    """Stable dedup key: sha256(job_id | time-bucket | family | kind)[:32]."""
    digest = hashlib.sha256(f"{job_id}|{event.key_seed}".encode("utf-8")).hexdigest()
    return digest[:32]


def from_row(row: dict[str, Any]) -> SignalEvent:
    """Re-materializes a stored row into a SignalEvent (fusion input)."""
    return SignalEvent(
        t=float(row["start_time"]),
        t_end=float(row["end_time"]),
        family=str(row["evidence"].get("family", "audio")),
        kind=str(row["evidence"].get("kind", "")),
        strength=float(row["evidence"].get("strength", 0.0)),
        evidence=dict(row["evidence"]),
    )
