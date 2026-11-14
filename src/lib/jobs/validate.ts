import { appConfig } from "@/config/app";
import { classifyVideoUrl } from "@/lib/validation/clips";
import { z } from "zod";

/**
 * Job-creation payload validation (server-side, authoritative).
 * The client validates the same rules for UX, but the API never trusts the
 * client — every request is re-validated here before touching the database.
 */

const { clips } = appConfig;

const httpUrl = z
  .string()
  .url()
  .refine((value) => value.startsWith("http://") || value.startsWith("https://"), {
    message: "must be an http(s) URL",
  });

const YOUTUBE_PATH = /\/(watch|shorts|live|embed)/;

/** Canonical Twitch VOD path shapes (strict; live/channel URLs are NOT VODs). */
const TWITCH_VOD_PATH = /^\/(?:[a-z0-9_]{1,25}\/)?videos\/\d{1,15}$/;

export const createJobSchema = z
  .object({
    sourceType: z.enum(["youtube", "twitch", "upload", "direct_url"]),
    youtubeUrl: httpUrl
      .optional()
      .refine((value) => {
        if (!value) return true;
        try {
          const url = new URL(value);
          const host = url.hostname.toLowerCase().replace(/^www\./, "");
          if (host === "youtu.be") return url.pathname.startsWith("/") && url.pathname !== "/";
          if (host === "youtube.com" || host === "m.youtube.com" || host === "music.youtube.com") {
            return YOUTUBE_PATH.test(url.pathname);
          }
          return false;
        } catch {
          return false;
        }
      }, "must be a YouTube URL")
      .optional(),
    twitchUrl: httpUrl
      .optional()
      .refine((value) => {
        if (!value) return true;
        try {
          const url = new URL(value);
          const host = url.hostname.toLowerCase().replace(/^www\./, "");
          if (host !== "twitch.tv" && host !== "m.twitch.tv") return false;
          return TWITCH_VOD_PATH.test(url.pathname);
        } catch {
          return false;
        }
      }, "must be a Twitch VOD URL")
      .optional(),
    /**
     * TASK 014 — universal "Video URL" input. A single pasted URL,
     * classified server-side (shape only) into youtube / twitch-vod /
     * twitch-clip / twitch-live / direct-file. The service layer applies
     * the honest capability gate per class (503 for what cannot be
     * imported through an authorized mechanism; job creation for what can).
     */
    videoUrl: httpUrl
      .optional()
      .refine((value) => value === undefined || classifyVideoUrl(value) !== "unrecognized", {
        message:
          "must be a YouTube URL, a Twitch VOD/clip URL, or a direct video file URL (.mp4/.webm/.mov)",
      })
      .optional(),
    fileName: z
      .string()
      .min(1)
      .max(255)
      .refine((value) => !value.includes("..") && !value.includes("/") && !value.includes("\\"), {
        message: "must be a plain file name",
      })
      .optional(),
    clipCount: z.number().int().min(clips.minCount).max(clips.maxCount),
    clipDurationSeconds: z.number().int().min(clips.minSeconds).max(clips.maxSeconds),
    captionsEnabled: z.boolean().default(true),
  })
  .refine(
    (data) => {
      if (data.sourceType === "youtube") return Boolean(data.youtubeUrl);
      if (data.sourceType === "twitch") return Boolean(data.twitchUrl ?? data.videoUrl);
      if (data.sourceType === "direct_url") {
        const videoUrl = data.videoUrl;
        return videoUrl !== undefined && classifyVideoUrl(videoUrl) === "direct-file";
      }
      return Boolean(data.fileName);
    },
    {
      message:
        "youtube sourceType requires youtubeUrl; twitch requires twitchUrl; " +
        "direct_url requires a direct video file URL (.mp4/.webm/.mov); upload requires fileName",
      path: ["sourceType"],
    }
  );

export type CreateJobInput = z.infer<typeof createJobSchema>;

export function validateCreateJobPayload(raw: unknown):
  | { success: true; data: CreateJobInput }
  | { success: false; error: string } {
  const result = createJobSchema.safeParse(raw);
  if (result.success) return { success: true, data: result.data };
  const first = result.error.issues[0];
  const field = first && first.path.length > 0 ? first.path.join(".") : "payload";
  return { success: false, error: `Invalid ${field}: ${first?.message ?? "bad value"}` };
}
