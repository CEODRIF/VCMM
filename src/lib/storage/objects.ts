import "server-only";

import { uploadConfig } from "@/config/uploads";
import { ApiError } from "@/lib/api/errors";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Private Supabase Storage object operations (server-only, service role).
 *
 * - Object paths are ALWAYS generated server-side.
 * - Signed upload URLs are created server-side and handed to the browser
 *   (the browser never sees the service-role key).
 * - No public URLs are ever created for source videos.
 */

export interface SourceObjectInfo {
  name: string;
  size: number;
  mimeType?: string;
  updatedAt?: string;
}

/**
 * Creates a signed upload URL for the given object path.
 * The path must have been generated server-side (see buildSourcePath).
 */
export async function createSourceUploadUrl(path: string): Promise<{ signedUrl: string }> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase.storage
    .from(uploadConfig.sourcesBucket)
    .createSignedUploadUrl(path, { upsert: false });
  if (error || !data) {
    // TASK 017: a storage API failure here used to surface as a generic
    // 500 "Internal server error." with no indication of what happened to
    // the file. Now typed + actionable — and explicit that NOTHING was
    // uploaded (no link was issued, so no transfer could have started).
    console.error("[storage] signed upload URL creation failed:", error?.message ?? "unknown");
    throw new ApiError(
      502,
      "Private storage is temporarily unavailable, so an upload link could not be issued. " +
        "Your file was NOT uploaded — please retry in a moment."
    );
  }
  return { signedUrl: data.signedUrl };
}

/** Server-side object path for a job's original upload. Never client-supplied. */
export function buildSourcePath(jobId: string, generatedFilename: string): string {
  return `sources/${jobId}/original/${generatedFilename}`;
}

/**
 * Verifies that an object exists at the expected path and returns its
 * metadata (size, MIME). Returns null when the object is missing.
 * Uses the storage list endpoint (metadata only — no byte transfer).
 */
export async function verifySourceObject(jobId: string, expectedPath: string | null): Promise<SourceObjectInfo | null> {
  if (!expectedPath) return null;
  const directory = expectedPath.substring(0, expectedPath.lastIndexOf("/"));
  const expectedName = expectedPath.substring(expectedPath.lastIndexOf("/") + 1);

  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase.storage
    .from(uploadConfig.sourcesBucket)
    .list(directory);
  if (error) {
    // TASK 017: typed + actionable. The transfer itself MAY have reached
    // storage, but it CANNOT be confirmed — the message never claims
    // success and never claims loss.
    console.error("[storage] object verification failed:", error.message);
    throw new ApiError(
      502,
      "Private storage is temporarily unavailable, so the uploaded file could not be verified. " +
        "The job was NOT completed — please retry the upload."
    );
  }

  const item = (data ?? []).find((entry) => entry.name === expectedName);
  if (!item) return null;

  // The storage list API reports size/MIME inside `metadata`.
  const size = item.metadata?.size;
  if (typeof size !== "number" || size <= 0) {
    // Metadata without a size cannot be verified — treat as unverified.
    return null;
  }
  return {
    name: item.name,
    size,
    mimeType: item.metadata?.mimetype,
    updatedAt: item.updated_at,
  };
}
