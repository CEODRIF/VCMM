/**
 * Centralized application configuration for ClipForge AI.
 *
 * Single source of truth for product constraints and branding.
 * Both the UI and the validation layer read from here so the rules
 * can never drift apart.
 */
export const appConfig = {
  name: "ClipForge AI",
  shortName: "ClipForge",
  tagline: "Long videos in. Short clips out.",
  description:
    "ClipForge AI turns long-form video into short vertical clips, with AI-driven clip detection and modern animated subtitles.",

  /** Product constraints for generated clips. */
  clips: {
    minSeconds: 60,
    maxSeconds: 180,
    defaultSeconds: 60,
    stepSeconds: 30,
    minCount: 1,
    maxCount: 10,
    defaultCount: 3,
  },

  /** Fixed output specification. */
  output: {
    aspectRatio: "9:16",
    width: 1080,
    height: 1920,
    resolutionLabel: "1080p",
  },

  /** Shared access code (server-side verification arrives in Phase 2). */
  access: {
    codeMinLength: 6,
    codeMaxLength: 128,
  },

  /** Ordered processing state machine. */
  processingStates: [
    "queued",
    "uploading",
    "transcribing",
    "analyzing",
    "rendering",
    "completed",
    "failed",
  ] as const,

  /** Landing page feature overview (facts about the product, no claims). */
  features: [
    {
      icon: "sparkles",
      title: "AI clip detection",
      description: "The pipeline analyzes your video and selects the strongest moments automatically.",
    },
    {
      icon: "film",
      title: "Vertical 9:16 output",
      description: "Every clip is rendered vertical at 1080x1920, ready for short-form platforms.",
    },
    {
      icon: "captions",
      title: "Modern animated subtitles",
      description: "Word-level animated captions built from a Whisper transcription of your audio.",
    },
    {
      icon: "youtube",
      title: "YouTube or direct upload",
      description: "Paste a YouTube URL (authorized source provider) or drop a video file directly.",
    },
    {
      icon: "scissors",
      title: "60-180 second clips",
      description: "Choose how many clips to create and the target duration between 60 and 180 seconds.",
    },
    {
      icon: "archive",
      title: "Individual and ZIP downloads",
      description: "Download clips one by one, or grab the whole batch as a single ZIP archive.",
    },
  ],
} as const;

export type AppConfig = typeof appConfig;
export type ProcessingState = (typeof appConfig.processingStates)[number];
