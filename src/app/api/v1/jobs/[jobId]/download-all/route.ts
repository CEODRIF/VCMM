import "server-only";

import { NextResponse } from "next/server";

import { uploadConfig } from "@/config/uploads";
import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { requireOwnedJob } from "@/lib/api/owned-job";
import { listClipsForJob, type ClipRow } from "@/lib/jobs/results";
import {
  fetchClipObjectStream,
  listJobClipObjects,
  validateClipPath,
} from "@/lib/storage/clips";
import { sanitizeZipEntryName } from "@/lib/zip/filename";
import { streamStoreZip, type StoreZipEntry } from "@/lib/zip/store-zip";

/**
 * GET /api/v1/jobs/[jobId]/download-all — "Download all" as a streaming ZIP.
 *
 * Guard sequence (JSON errors BEFORE any archive byte is sent):
 *   session + ownership → job is `completed` (else 409) → at least one
 *   `ready` clip with a canonical storage path → ALL of those clip objects
 *   exist in Storage (else 409) → total size within the configured cap
 *   (else 413, with a "download individually" fallback message).
 *
 * Streaming model (no large bytes in memory or on disk):
 *   - The ZIP is written with the STORE method by an async generator
 *     (src/lib/zip/store-zip.ts); memory stays at a single fetch chunk
 *     regardless of archive size;
 *   - each clip is fetched from Storage with the service key and piped
 *     chunk by chunk; concurrency is exactly 1, in sort order — bounded
 *     and deterministic;
 *   - entry names are sanitized server-side (single path segment, index
 *     prefix for uniqueness) — path traversal is impossible;
 *   - client cancellation (or a mid-stream Storage failure) aborts the
 *     in-flight fetch and tears down the response. A partially downloaded
 *     archive cannot be guaranteed complete, so the client discards it —
 *     the per-clip downloads remain the reliable fallback.
 *
 * Hosting limitation (documented, no paid infra added): serverless
 * execution time and bandwidth are bounded, so the total-size cap makes
 * oversized sets fail FAST with a clear 413 instead of aborting
 * mid-download.
 */
export async function GET(request: Request, context: { params: { jobId: string } }) {
  try {
    const job = await requireOwnedJob(request, context.params.jobId);

    if (job.status !== "completed") {
      throw new ApiError(409, "Your clips are still being processed. Downloading is available once the job completes.");
    }

    // Only ready clips with a canonical path for THIS (job, clip) pair.
    const clips = (await listClipsForJob(job.id)).filter(
      (c) => c.status === "ready" && validateClipPath(c.storage_path, job.id, c.id)
    );
    if (clips.length === 0) {
      throw new ApiError(409, "No clips are available to download yet.");
    }

    // One metadata-only listing for the whole job: existence + sizes.
    const objects = await listJobClipObjects(job.id);
    const missing = clips.filter((c) => !objects.has(`${c.id}.mp4`));
    if (missing.length > 0) {
      throw new ApiError(409, "Some clips could not be found in storage. Try again later, or download the available clips individually.");
    }

    const totalBytes = clips.reduce((n, c) => n + (objects.get(`${c.id}.mp4`)?.size ?? 0), 0);
    if (totalBytes > uploadConfig.maxZipBytes()) {
      throw new ApiError(413, "All clips together exceed the ZIP size limit. Download them individually instead.");
    }

    // Cancellation plumbing: the client-visible request signal AND the
    // response stream's cancel() both abort the in-flight Storage fetch.
    const abort = new AbortController();
    if (request.signal.aborted) abort.abort();
    request.signal.addEventListener("abort", () => abort.abort(), { once: true });

    const entries: StoreZipEntry[] = clips.map((clip, index) => ({
      name: sanitizeZipEntryName(clip.title, index),
      source: streamClipObject(job.id, clip, abort.signal),
    }));

    const zipStream = new ReadableStream<Uint8Array>({
      async start(controller) {
        try {
          for await (const chunk of streamStoreZip(entries)) {
            controller.enqueue(chunk);
          }
          controller.close();
        } catch (error) {
          abort.abort();
          controller.error(error);
        }
      },
      cancel() {
        abort.abort();
      },
    });

    return new Response(zipStream, {
      status: 200,
      headers: {
        "content-type": "application/zip",
        "content-disposition": `attachment; filename="clips-${job.id}.zip"`,
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}

/**
 * Streams one verified clip object, chunk by chunk. LAZY: the Storage
 * fetch only starts when the ZIP writer reaches this entry, which is what
 * keeps concurrency at exactly 1 in the deterministic sort order.
 */
async function* streamClipObject(
  jobId: string,
  clip: ClipRow,
  signal: AbortSignal
): AsyncGenerator<Uint8Array> {
  const body = await fetchClipObjectStream({ jobId, clipId: clip.id }, signal);
  const reader = body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) yield value;
  }
}
