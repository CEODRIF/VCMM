import "server-only";

import { ApiError } from "@/lib/api/errors";

/**
 * Authorized Twitch source-provider interface (Phase 8, TASK 011).
 *
 * Policy: ClipForge only retrieves Twitch VODs through an AUTHORIZED source
 * provider (e.g. the Twitch Helix API where the owner grants access, or a
 * licensed ingestion service). No unauthorized cookies, no private
 * endpoints, no circumvention — by design, ever.
 *
 * Capability honesty (TASK 011 → updated by TASK 014):
 *
 * - VOD media: the official Twitch APIs expose NO VOD media-download
 *   endpoint. The API refuses VOD imports with an actionable 503
 *   (export from the dashboard, or upload the file directly).
 * - CLIP media: TASK 014 added the official Get Clips Download path —
 *   a creator-owned authorization (the creator's OAuth refresh token,
 *   configured on the WORKER) that lets the worker download clips
 *   through `GET /helix/clips/downloads`. That capability truth lives
 *   in the worker (where the credentials are), so job creation for
 *   CLIP URLs is no longer gated here; an unconfigured worker fails the
 *   job with a clear, permanent, actionable error. Live streams and
 *   VOD URLs are refused at the API with distinct 503s.
 *
 * When no authorized provider (and its credentials) is configured,
 * {@link getTwitchProvider} returns an explicitly unavailable provider;
 * direct file uploads remain fully functional in every configuration.
 */

export interface TwitchSourceMetadata {
  /** Server-controlled storage path where the provider materialized the video. */
  sourcePath: string;
  durationSeconds: number;
  title?: string;
}

export interface TwitchSourceProvider {
  readonly name: string;
  /** Whether an authorized provider is configured and usable. */
  isAvailable(): boolean;
  /**
   * Whether the provider can MATERIALIZE video media (not just metadata)
   * through an authorized, policy-compliant mechanism. False for every
   * implementation that ships with this codebase (see module docstring).
   */
  supportsMaterialization(): boolean;
  /**
   * Fetches an authorized source video for the given URL and returns its
   * metadata. Must never run in the browser.
   */
  fetchSource(url: string): Promise<TwitchSourceMetadata>;
}

/** Returned while no authorized provider is configured. */
class UnconfiguredTwitchProvider implements TwitchSourceProvider {
  readonly name = "unconfigured";

  isAvailable(): boolean {
    // A provider becomes available only when an operator explicitly configures
    // an authorized integration (name + credentials) in the server environment.
    // No such integration ships with this codebase.
    return false;
  }

  supportsMaterialization(): boolean {
    // Honest capability: no authorized media-download mechanism exists in
    // this codebase (official APIs expose none; no circumvention by design).
    return false;
  }

  async fetchSource(_url: string): Promise<TwitchSourceMetadata> {
    throw new ApiError(
      503,
      "Twitch source provider is not configured on this deployment. Direct video file upload is available."
    );
  }
}

const unavailable = new UnconfiguredTwitchProvider();

/**
 * Returns the active Twitch provider. To enable a real integration, this is
 * the single extension point: implement TwitchSourceProvider against an
 * authorized service and return it here when its credentials are configured.
 */
export function getTwitchProvider(): TwitchSourceProvider {
  return unavailable;
}
