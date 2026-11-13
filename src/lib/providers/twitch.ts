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
 * Capability honesty (TASK 011): the official Twitch APIs (Helix /videos,
 * /streams) expose METADATA (availability, duration, title) but NO
 * media-download endpoint. ``supportsMaterialization`` therefore reports
 * false for every honest implementation until a separately licensed
 * ingestion path is explicitly approved and wired — metadata capability
 * never implies materialization capability.
 *
 * When no authorized provider (and its credentials) is configured,
 * {@link getTwitchProvider} returns an explicitly unavailable provider and
 * the API answers with a clear 503 "provider unavailable" response while
 * direct file uploads remain fully functional.
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
