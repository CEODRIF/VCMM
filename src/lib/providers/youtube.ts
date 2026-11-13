import "server-only";

import { ApiError } from "@/lib/api/errors";

/**
 * Authorized YouTube source-provider interface (Phase 3).
 *
 * Policy: ClipForge only retrieves YouTube videos through an AUTHORIZED
 * source provider (e.g. the YouTube Data API / Content API where the owner
 * grants access, or a licensed ingestion service). No downloader of record
 * (yt-dlp-style), no browser-cookie reuse, no DRM bypass — by design, ever.
 *
 * When no authorized provider (and its credentials) is configured,
 * {@link getYouTubeProvider} returns an explicitly unavailable provider and
 * the API answers with a clear 503 "provider unavailable" response while
 * direct file uploads remain fully functional.
 */

export interface YouTubeSourceMetadata {
  /** Server-controlled storage path where the provider materialized the video. */
  sourcePath: string;
  durationSeconds: number;
  title?: string;
}

export interface YouTubeSourceProvider {
  readonly name: string;
  /** Whether an authorized provider is configured and usable. */
  isAvailable(): boolean;
  /**
   * Fetches an authorized source video for the given URL and returns its
   * metadata. Must never run in the browser.
   */
  fetchSource(url: string): Promise<YouTubeSourceMetadata>;
}

/** Returned while no authorized provider is configured. */
class UnconfiguredYouTubeProvider implements YouTubeSourceProvider {
  readonly name = "unconfigured";

  isAvailable(): boolean {
    // A provider becomes available only when an operator explicitly configures
    // an authorized integration (name + credentials) in the server environment.
    // No such integration ships with this codebase.
    return false;
  }

  async fetchSource(_url: string): Promise<YouTubeSourceMetadata> {
    throw new ApiError(
      503,
      "YouTube source provider is not configured on this deployment. Direct video file upload is available."
    );
  }
}

const unavailable = new UnconfiguredYouTubeProvider();

/**
 * Returns the active YouTube provider. To enable a real integration, this is
 * the single extension point: implement YouTubeSourceProvider against an
 * authorized service and return it here when its credentials are configured.
 */
export function getYouTubeProvider(): YouTubeSourceProvider {
  return unavailable;
}
