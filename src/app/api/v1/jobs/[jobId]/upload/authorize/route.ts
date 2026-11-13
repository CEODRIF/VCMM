import "server-only";

import { randomUUID } from "node:crypto";
import { NextResponse } from "next/server";

import { requireSession } from "@/lib/api/auth";
import { ApiError, toErrorResponse } from "@/lib/api/errors";
import { findJobForSession, setUploadAuthorization } from "@/lib/db/jobs";
import { uploadConfig } from "@/config/uploads";
import { getClientIp, SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { buildSourcePath, createSourceUploadUrl } from "@/lib/storage/objects";
import { z } from "zod";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Per-process limiter for upload-authorization requests (keyed by
 * session + client IP). Prevents signed-URL churn / storage-path spam.
 */
const authorizeLimiter = new SlidingWindowRateLimiter({
  windowMs: uploadConfig.authorizeRateLimit.windowMs,
  max: uploadConfig.authorizeRateLimit.maxAttempts,
});

const authorizeSchema = z.object({
  fileName: z
    .string()
    .min(1)
    .max(255)
    .refine((v) => !v.includes("..") && !v.includes("/") && !v.includes("\\"), {
      message: "must be a plain file name",
    }),
  sizeBytes: z.number().int().positive(),
  mimeType: z.string().min(1),
});

function extensionOf(fileName: string): string | null {
  const match = fileName.trim().toLowerCase().match(/\.([a-z0-9]+)$/);
  return match?.[1] ?? null;
}

/**
 * POST /api/v1/jobs/[jobId]/upload/authorize
 *
 * Issues a SHORT-LIVED signed upload authorization for a job the caller
 * owns. All storage paths are generated server-side; the client receives
 * only the signed URL it may PUT the file to.
 */
export async function POST(request: Request, context: { params: { jobId: string } }) {
  try {
    const jobId = context.params.jobId;
    if (!UUID_PATTERN.test(jobId)) {
      throw new ApiError(404, "Job not found.");
    }

    const session = await requireSession(request);

    const limit = authorizeLimiter.check(`${session.sessionId}:${getClientIp(request)}`);
    if (limit.limited) {
      throw new ApiError(429, "Too many upload requests. Try again shortly.");
    }

    const job = await findJobForSession(session.sessionId, jobId);
    if (!job) {
      throw new ApiError(404, "Job not found.");
    }
    if (job.source_type !== "upload") {
      throw new ApiError(400, "This job was not created for file upload.");
    }
    if (job.status !== "uploading") {
      throw new ApiError(409, `Job is not in the uploading state (current: ${job.status}).`);
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      throw new ApiError(400, "Invalid JSON payload.");
    }
    const parsed = authorizeSchema.safeParse(body);
    if (!parsed.success) {
      throw new ApiError(400, `Invalid payload: ${parsed.error.issues[0]?.message ?? "bad value"}`);
    }
    const { fileName, sizeBytes, mimeType } = parsed.data;

    // Coarse pre-filter (the worker re-verifies actual bytes with FFprobe).
    const extension = extensionOf(fileName);
    const expectedMime = extension ? uploadConfig.allowedExtensions[extension as keyof typeof uploadConfig.allowedExtensions] : undefined;
    if (!extension || !expectedMime) {
      throw new ApiError(415, "Unsupported file type. Upload MP4, MOV, or WEBM videos.");
    }
    if (!uploadConfig.allowedMimeTypes.has(mimeType.toLowerCase())) {
      throw new ApiError(415, "Unsupported MIME type for video upload.");
    }
    // MIME-spoofing guard: the declared MIME must match the extension class.
    if (mimeType.toLowerCase() !== expectedMime) {
      throw new ApiError(400, "File extension and MIME type do not match.");
    }
    const maxBytes = uploadConfig.maxUploadSizeBytes();
    if (sizeBytes > maxBytes) {
      throw new ApiError(
        413,
        `File is too large. Maximum upload size is ${Math.round(maxBytes / (1024 * 1024))} MB.`
      );
    }

    // Server-generated object path (never client-supplied).
    const generatedFilename = `${randomUUID()}.${extension}`;
    const objectPath = buildSourcePath(jobId, generatedFilename);

    const { signedUrl } = await createSourceUploadUrl(objectPath);

    const ttlSeconds = uploadConfig.uploadAuthTtlSeconds();
    const authExpiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();

    const updated = await setUploadAuthorization(session.sessionId, jobId, objectPath, authExpiresAt, {
      fileName,
      sizeBytes,
      mimeType,
    });
    if (updated === 0) {
      // Lost a race (job left the uploading state).
      throw new ApiError(409, "Job state changed. Refresh and try again.");
    }

    return NextResponse.json(
      {
        uploadUrl: signedUrl,
        method: "PUT",
        contentType: mimeType.toLowerCase(),
        maxBytes,
        expiresInSeconds: ttlSeconds,
        // Path of the job, not the storage object — the client needs no
        // knowledge of the storage layout.
        jobId,
      },
      { status: 200 }
    );
  } catch (error) {
    const res = toErrorResponse(error);
    return NextResponse.json(res.body, { status: res.status });
  }
}
