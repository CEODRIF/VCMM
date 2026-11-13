import { appConfig } from "@/config/app";
import type { ValidationResult } from "@/types";

const { minSeconds, maxSeconds, minCount, maxCount } = appConfig.clips;

/**
 * Validates a requested clip duration against the product rules:
 * whole number of seconds, between 60 and 180 inclusive.
 */
export function validateClipDuration(seconds: number): ValidationResult {
  if (!Number.isFinite(seconds)) {
    return { valid: false, error: "Clip duration must be a number." };
  }
  if (!Number.isInteger(seconds)) {
    return { valid: false, error: "Clip duration must be a whole number of seconds." };
  }
  if (seconds < minSeconds) {
    return { valid: false, error: `Clip duration must be at least ${minSeconds} seconds.` };
  }
  if (seconds > maxSeconds) {
    return { valid: false, error: `Clip duration must be at most ${maxSeconds} seconds.` };
  }
  return { valid: true };
}

/** Validates a requested clip count against the product rules: 1-10. */
export function validateClipCount(count: number): ValidationResult {
  if (!Number.isFinite(count)) {
    return { valid: false, error: "Clip count must be a number." };
  }
  if (!Number.isInteger(count)) {
    return { valid: false, error: "Clip count must be a whole number." };
  }
  if (count < minCount) {
    return { valid: false, error: `Create at least ${minCount} clip.` };
  }
  if (count > maxCount) {
    return { valid: false, error: `Create at most ${maxCount} clips.` };
  }
  return { valid: true };
}

const YOUTUBE_HOSTS = new Set([
  "youtube.com",
  "www.youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtu.be",
]);

/** Validates that a string is a plausible YouTube video URL. */
export function validateYoutubeUrl(url: string): ValidationResult {
  const trimmed = url.trim();
  if (trimmed === "") {
    return { valid: false, error: "Paste a YouTube URL or upload a video file." };
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { valid: false, error: "Enter a valid URL, e.g. https://youtube.com/watch?v=..." };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { valid: false, error: "Only http(s) YouTube URLs are supported." };
  }
  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  if (!YOUTUBE_HOSTS.has(host) && !YOUTUBE_HOSTS.has(`www.${host}`)) {
    return { valid: false, error: "That does not look like a YouTube URL." };
  }
  if (host === "youtu.be") {
    if (!parsed.pathname.startsWith("/") || parsed.pathname === "/") {
      return { valid: false, error: "YouTube Short links need a video id, e.g. https://youtu.be/abc123" };
    }
  } else if (!/\/(watch|shorts|live|embed)/.test(parsed.pathname) && parsed.searchParams.has("v")) {
    // watch?v=... is covered by /watch; bare ?v= on the root is allowed too.
  } else if (!/\/(watch|shorts|live|embed)/.test(parsed.pathname)) {
    return { valid: false, error: "Use a watch, shorts, or youtu.be link." };
  }
  return { valid: true };
}

const TWITCH_HOSTS = new Set(["twitch.tv", "m.twitch.tv"]);
/** Canonical Twitch VOD paths: /videos/{id} or /{channel}/videos/{id}. */
const TWITCH_VOD_PATH = /^\/(?:[a-z0-9_]{1,25}\/)?videos\/\d{1,15}$/;

/** Validates that a string is a canonical Twitch VOD URL (client-side UX). */
export function validateTwitchUrl(url: string): ValidationResult {
  const trimmed = url.trim();
  if (trimmed === "") {
    return { valid: false, error: "Paste a Twitch VOD URL or upload a video file." };
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { valid: false, error: "Enter a valid URL, e.g. https://twitch.tv/videos/123456789" };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { valid: false, error: "Only http(s) Twitch URLs are supported." };
  }
  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  if (!TWITCH_HOSTS.has(host)) {
    return { valid: false, error: "That does not look like a Twitch URL." };
  }
  if (!TWITCH_VOD_PATH.test(parsed.pathname)) {
    return {
      valid: false,
      error: "Use a VOD link, e.g. https://twitch.tv/videos/123456789 — live streams are not available yet.",
    };
  }
  return { valid: true };
}
