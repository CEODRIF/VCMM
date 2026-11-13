import "server-only";

import { uploadConfig } from "@/config/uploads";
import { getSupabaseCredentials, getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Private clip-object operations (Phase 6). Server-only, service role.
 *
 * SECURITY MODEL:
 * - Object paths are ALWAYS derived server-side from the verified
 *   (job_id, clip_id) pair — the canonical shape `clips/{job_id}/{clip_id}.mp4`
 *   (the exact convention the worker writes). A `storage_path` stored in the
 *   DB is CROSS-CHECKED against that shape and the owning job; on any
 *   mismatch the clip is treated as undownloadable, never trusted blindly.
 * - The buckets stay PRIVATE. Access is granted exclusively through
 *   short-lived signed URLs minted here after session + ownership checks.
 * - No public URLs, no bucket policy changes, no client-visible keys.
 */

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const CLIP_PATH_RE = new RegExp(`^clips/(${UUID})/(${UUID})\\.mp4$`, "i");

export interface ClipObjectRef {
  jobId: string;
  clipId: string;
}

/** The canonical object path for a clip (derived, never client-supplied). */
export function clipObjectPath(jobId: string, clipId: string): string {
  return `clips/${jobId}/${clipId}.mp4`;
}

/**
 * Validates a stored `storage_path` against the canonical convention AND
 * the owning job. Returns null when the path is missing, malformed, or
 * points at a different job (defense in depth — the row was already
 * job-scoped, but a corrupt/foreign path must not be signed or read).
 */
export function validateClipPath(storagePath: string | null, jobId: string, clipId: string): boolean {
  if (!storagePath) return false;
  const match = CLIP_PATH_RE.exec(storagePath);
  if (!match) return false;
  const jobPart = match[1];
  const clipPart = match[2];
  return jobPart !== undefined && clipPart !== undefined
    && jobPart.toLowerCase() === jobId.toLowerCase()
    && clipPart.toLowerCase() === clipId.toLowerCase();
}

export interface SignedClipUrl {
  signedUrl: string;
  /** The URL's validity window, in seconds (documented for the UI). */
  expiresInSeconds: number;
}

/**
 * Mints a SHORT-LIVED signed URL for one verified clip object.
 * `download: true` (with a filename) forces a browser download; the
 * default streams/inline (video previews).
 */
export async function createClipSignedUrl(
  ref: ClipObjectRef,
  expiresIn: number,
  options?: { download?: boolean; fileName?: string }
): Promise<SignedClipUrl> {
  const supabase = getSupabaseServerClient();
  const path = clipObjectPath(ref.jobId, ref.clipId);
  const { data, error } = await supabase.storage
    .from(uploadConfig.clipsBucket)
    .createSignedUrl(path, expiresIn, options?.download ? { download: options.fileName ?? "clip.mp4" } : {});
  if (error || !data) {
    throw new Error(`Signed URL creation failed: ${error?.message ?? "unknown"}`);
  }
  return { signedUrl: data.signedUrl, expiresInSeconds: expiresIn };
}

export interface ClipObjectInfo {
  name: string;
  size: number;
  mimeType?: string;
  updatedAt?: string;
}

/**
 * Metadata-only listing of one job's clip objects (no byte transfer).
 * Returns the map of `{ "<clipId>.mp4": info }` for the job directory —
 * one call serves the whole job (existence checks + ZIP size cap).
 */
export async function listJobClipObjects(jobId: string): Promise<Map<string, ClipObjectInfo>> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase.storage
    .from(uploadConfig.clipsBucket)
    .list(`clips/${jobId}`);
  if (error) {
    throw new Error(`Clip listing failed: ${error.message}`);
  }
  const out = new Map<string, ClipObjectInfo>();
  for (const item of data ?? []) {
    const size = item.metadata?.size;
    if (typeof size !== "number" || size <= 0) continue; // unverifiable entries are skipped
    out.set(item.name, {
      name: item.name,
      size,
      mimeType: item.metadata?.mimetype,
      updatedAt: item.updated_at,
    });
  }
  return out;
}

/**
 * Streams ONE clip object from Storage as a web ReadableStream — TRUE
 * streaming: the object is fetched with the service key over the Storage
 * REST API and its body piped through; nothing is buffered in memory as a
 * whole (chunks only). Aborts with the caller's signal (client cancel).
 *
 * Throws on 404 (object vanished), non-200 responses, or network failure —
 * the ZIP route treats any of these as "stop, the archive cannot be
 * guaranteed complete" and the browser's request is aborted.
 */
export async function fetchClipObjectStream(
  ref: ClipObjectRef,
  signal?: AbortSignal
): Promise<ReadableStream<Uint8Array>> {
  const { url, serviceRoleKey } = getSupabaseCredentials();
  const path = clipObjectPath(ref.jobId, ref.clipId);
  const res = await fetch(`${url}/storage/v1/object/${uploadConfig.clipsBucket}/${path}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${serviceRoleKey}` },
    signal,
  });
  if (!res.ok) {
    throw new Error(`Clip object fetch failed: ${res.status}`);
  }
  if (!res.body) {
    throw new Error("Clip object fetch returned no body");
  }
  return res.body as ReadableStream<Uint8Array>;
}
