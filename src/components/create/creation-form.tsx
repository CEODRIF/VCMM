"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CheckCircle2, Info, Link2, Loader2, RefreshCw, Square, Twitch, Upload, Wand2, Youtube } from "lucide-react";

import { appConfig } from "@/config/app";
import type { SourceType } from "@/types";
import { cn, formatBytes } from "@/lib/utils";
import { classifyVideoUrl, validateClipCount, validateClipDuration, validateTwitchUrl, validateYoutubeUrl } from "@/lib/validation/clips";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { UploadArea } from "@/components/create/upload-area";

const { clips, output } = appConfig;

const DURATION_OPTIONS = Array.from(
  { length: Math.floor((clips.maxSeconds - clips.minSeconds) / clips.stepSeconds) + 1 },
  (_, index) => clips.minSeconds + index * clips.stepSeconds
);

const COUNT_OPTIONS = Array.from(
  { length: clips.maxCount - clips.minCount + 1 },
  (_, index) => clips.minCount + index
);

interface FieldErrors {
  source?: string;
  clipCount?: string;
  duration?: string;
}

type UploadStage = "idle" | "transferring" | "completing";

/**
 * Main creation form — connected to the real job + upload APIs (Phase 3).
 *
 * Upload flow (all server-verified, no bytes through Vercel):
 *   1. POST /api/v1/jobs                    → job row (status: uploading)
 *   2. POST /api/v1/jobs/{id}/upload/authorize → signed URL (server path)
 *   3. PUT {signedUrl}  (browser → Supabase Storage, real progress + cancel)
 *   4. POST /api/v1/jobs/{id}/upload/complete  → object verified → job queued
 *   5. Live status via GET /api/v1/jobs/{id} (processing page)
 */
/** The four source TABS. "video" is the TASK 014 universal link tab —
 * its pasted URL is classified and the job is created with the REAL
 * source type (youtube / twitch / direct_url). */
type SourceTab = SourceType | "video";

const VIDEO_TAB_HINTS: Record<string, { tone: "ok" | "warn"; text: string }> = {
  "direct-file": {
    tone: "ok",
    text: "Direct video file link — the server downloads it through the secure import path (public files only, size and time limits apply).",
  },
  "twitch-clip": {
    tone: "ok",
    text: "Twitch clip — imported via the official Get Clips Download endpoint when creator authorization is configured on the server.",
  },
  youtube: {
    tone: "warn",
    text: "YouTube link — the official YouTube APIs expose metadata only (no authorized media download). To import a YouTube video, upload the file directly.",
  },
  "twitch-vod": {
    tone: "warn",
    text: "Twitch VOD — there is no official VOD media-download endpoint. Export the VOD from the Twitch dashboard and upload the file directly.",
  },
  "twitch-live": {
    tone: "warn",
    text: "Live stream — not a completed recording. Wait for the VOD, or upload the file directly.",
  },
};

export function CreationForm() {
  const router = useRouter();

  const [sourceType, setSourceType] = React.useState<SourceTab>("youtube");
  const [youtubeUrl, setYoutubeUrl] = React.useState("");
  const [twitchUrl, setTwitchUrl] = React.useState("");
  const [videoUrl, setVideoUrl] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [clipCount, setClipCount] = React.useState<number>(clips.defaultCount);
  const [duration, setDuration] = React.useState<number>(clips.defaultSeconds);
  const [captionsEnabled, setCaptionsEnabled] = React.useState(true);

  const [errors, setErrors] = React.useState<FieldErrors>({});
  const [submitting, setSubmitting] = React.useState(false);
  const [authRequired, setAuthRequired] = React.useState(false);
  const [serverError, setServerError] = React.useState<string | null>(null);
  const [createdJobId, setCreatedJobId] = React.useState<string | null>(null);

  // Real upload state (no fake progress).
  const [uploadStage, setUploadStage] = React.useState<UploadStage>("idle");
  const [uploadProgress, setUploadProgress] = React.useState(0);
  const xhrRef = React.useRef<XMLHttpRequest | null>(null);

  const busy = submitting || uploadStage !== "idle";

  function resetTransientState() {
    setAuthRequired(false);
    setServerError(null);
    setCreatedJobId(null);
  }

  function abortUpload() {
    xhrRef.current?.abort();
    xhrRef.current = null;
    setUploadStage("idle");
    setUploadProgress(0);
  }

  function extensionOf(name: string): string {
    const match = name.toLowerCase().match(/\.([a-z0-9]+)$/);
    return match?.[1] ?? "";
  }

  function mimeForFile(candidate: File): string {
    if (candidate.type && candidate.type.startsWith("video/")) return candidate.type;
    const byExt: Record<string, string> = {
      mp4: "video/mp4",
      mov: "video/quicktime",
      webm: "video/webm",
    };
    return byExt[extensionOf(candidate.name)] ?? "application/octet-stream";
  }

  /** Step 2+3+4 for an already-created upload job. Returns true on success. */
  async function runUpload(jobId: string): Promise<boolean> {
    if (!file) return false;
    try {
      // 2) Authorize (server generates the storage path + signed URL).
      setUploadStage("transferring");
      setUploadProgress(0);
      const authResponse = await fetch(`/api/v1/jobs/${jobId}/upload/authorize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fileName: file.name, sizeBytes: file.size, mimeType: mimeForFile(file) }),
      });
      const authData = (await authResponse.json().catch(() => null)) as {
        uploadUrl?: string;
        contentType?: string;
        error?: string;
      } | null;
      if (!authResponse.ok || !authData?.uploadUrl) {
        setUploadStage("idle");
        setServerError(authData?.error ?? "Could not authorize the upload. Try again.");
        return false;
      }

      // 3) Direct browser → private Supabase Storage transfer (real progress).
      const uploaded = await putFile(authData.uploadUrl, authData.contentType ?? "application/octet-stream", file);
      if (!uploaded) {
        setUploadStage("idle");
        return false; // error/cancel already surfaced
      }

      // 4) Completion verification (server checks the real object + size).
      setUploadStage("completing");
      const completeResponse = await fetch(`/api/v1/jobs/${jobId}/upload/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sizeBytes: file.size }),
      });
      const completeData = (await completeResponse.json().catch(() => null)) as { error?: string } | null;
      if (!completeResponse.ok) {
        setUploadStage("idle");
        setServerError(completeData?.error ?? "Upload verification failed. Try the upload again.");
        return false;
      }
      setUploadStage("idle");
      setUploadProgress(0);
      return true;
    } catch {
      setUploadStage("idle");
      setServerError("Network error during upload. Check your connection and retry.");
      return false;
    }
  }

  /** XHR PUT so the UI can show real progress and cancel. */
  function putFile(url: string, contentType: string, payload: File): Promise<boolean> {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhrRef.current = xhr;
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", contentType);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          setUploadProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      xhr.onload = () => {
        xhrRef.current = null;
        if (xhr.status >= 200 && xhr.status < 300) resolve(true);
        else {
          setServerError("The storage transfer failed. Retry the upload.");
          resolve(false);
        }
      };
      xhr.onerror = () => {
        xhrRef.current = null;
        setServerError("The storage transfer failed. Check your connection and retry.");
        resolve(false);
      };
      xhr.onabort = () => {
        xhrRef.current = null;
        setServerError("Upload cancelled. You can start the upload again.");
        resolve(false);
      };
      xhr.send(payload);
    });
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    resetTransientState();
    setErrors({});

    // 1) Client-side validation (the server re-validates everything).
    const nextErrors: FieldErrors = {};
    const linkKind = sourceType === "video" ? classifyVideoUrl(videoUrl) : null;
    if (sourceType === "youtube") {
      const urlCheck = validateYoutubeUrl(youtubeUrl);
      if (!urlCheck.valid) nextErrors.source = urlCheck.error;
    } else if (sourceType === "twitch") {
      const urlCheck = validateTwitchUrl(twitchUrl);
      if (!urlCheck.valid) nextErrors.source = urlCheck.error;
    } else if (sourceType === "video") {
      if (!videoUrl.trim()) {
        nextErrors.source = "Paste a video URL.";
      } else if (linkKind === "unrecognized") {
        nextErrors.source =
          "Unrecognized link. Paste a YouTube URL, a Twitch VOD/clip URL, or a direct link ending in .mp4, .webm or .mov.";
      }
    } else if (!file) {
      nextErrors.source = "Drop a video file or pick one from your device.";
    }
    const countCheck = validateClipCount(clipCount);
    if (!countCheck.valid) nextErrors.clipCount = countCheck.error;
    const durationCheck = validateClipDuration(duration);
    if (!durationCheck.valid) nextErrors.duration = durationCheck.error;
    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) return;

    setSubmitting(true);
    try {
      // Step 1: create the job (no session/ownership data — cookie only).
      const response = await fetch("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
           body: JSON.stringify({
             // TASK 014: the video tab submits the CLASSIFIED real source
             // type (youtube → honest 503 refusal server-side; twitch
             // → per-class gate: clips queued, VOD/live actionable 503;
             // direct-file → queued as direct_url).
             sourceType:
               sourceType === "video"
                 ? linkKind === "direct-file"
                   ? "direct_url"
                   : linkKind === "youtube"
                     ? "youtube"
                     : "twitch"
                 : sourceType,
             youtubeUrl:
               sourceType === "youtube"
                 ? youtubeUrl.trim()
                 : sourceType === "video" && linkKind === "youtube"
                   ? videoUrl.trim()
                   : undefined,
             twitchUrl: sourceType === "twitch" ? twitchUrl.trim() : undefined,
             videoUrl:
               sourceType === "video" && linkKind !== "youtube" ? videoUrl.trim() : undefined,
             fileName: sourceType === "upload" ? file?.name : undefined,
             clipCount,
             clipDurationSeconds: duration,
             captionsEnabled,
           }),
      });
      const data = (await response.json().catch(() => null)) as {
        error?: string;
        job?: { id: string; status: string };
      } | null;

      if (response.status === 401) {
        setAuthRequired(true);
        return;
      }
        if (!response.ok || !data?.job) {
        // Includes 503 "YouTube/Twitch provider not configured" (or
        // "materialization not supported") with a clear, safe message —
        // no job was created, so no misleading job state can exist.
        setServerError(data?.error ?? "Could not create the job. Try again.");
        return;
      }

      const jobId = data.job.id;
      setCreatedJobId(jobId);

      if (sourceType === "upload") {
        const ok = await runUpload(jobId);
        if (ok) {
          router.push(`/processing?job=${jobId}`);
          return;
        }
        // Stay on the page: the job exists (status: uploading) and the user
        // can retry the transfer safely.
        return;
      }

      router.push(`/processing?job=${jobId}`);
    } catch {
      setServerError("Network error. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const canRetryUpload = createdJobId !== null && sourceType === "upload" && !busy;

  return (
    <form onSubmit={handleSubmit} noValidate className="grid gap-6 lg:grid-cols-[1fr_340px]">
      <Card className="card-surface animate-fade-in">
        <CardHeader>
          <CardTitle>Source</CardTitle>
          <CardDescription>
            Paste a YouTube or Twitch URL, a direct video file link, or upload a video file.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex flex-wrap gap-2" role="tablist" aria-label="Source type">
            {(
              [
                { value: "youtube", label: "YouTube URL", icon: <Youtube size={15} /> },
                { value: "twitch", label: "Twitch VOD", icon: <Twitch size={15} /> },
                { value: "video", label: "Video URL", icon: <Link2 size={15} /> },
                { value: "upload", label: "Upload file", icon: <Upload size={15} /> },
              ] as const
            ).map((tab) => (
              <button
                key={tab.value}
                type="button"
                role="tab"
                aria-selected={sourceType === tab.value}
                disabled={busy}
                onClick={() => {
                  setSourceType(tab.value);
                  setErrors((previous) => ({ ...previous, source: undefined }));
                  resetTransientState();
                }}
                className={cn(
                  "flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors disabled:opacity-60",
                  sourceType === tab.value
                    ? "border-primary/50 bg-primary/15 text-accent-foreground"
                    : "border-input bg-background/40 text-muted-foreground hover:text-foreground"
                )}
              >
                {tab.icon}
                {tab.label}
              </button>
            ))}
          </div>

          {sourceType === "youtube" ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="youtube-url">YouTube URL</Label>
              <Input
                id="youtube-url"
                type="url"
                inputMode="url"
                placeholder="https://youtube.com/watch?v=..."
                value={youtubeUrl}
                disabled={busy}
                onChange={(event) => {
                  setYoutubeUrl(event.target.value);
                  setErrors((previous) => ({ ...previous, source: undefined }));
                  resetTransientState();
                }}
                aria-invalid={errors.source ? true : undefined}
                className={cn(errors.source && "border-destructive focus-visible:ring-destructive")}
              />
              {errors.source && (
                <p className="text-sm text-destructive" role="alert">
                  {errors.source}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                YouTube sources require an authorized source provider; if none is configured on this
                deployment you will get a clear message, and direct upload always works.
              </p>
            </div>
          ) : sourceType === "twitch" ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="twitch-url">Twitch VOD URL</Label>
              <Input
                id="twitch-url"
                type="url"
                inputMode="url"
                placeholder="https://twitch.tv/videos/123456789"
                value={twitchUrl}
                disabled={busy}
                onChange={(event) => {
                  setTwitchUrl(event.target.value);
                  setErrors((previous) => ({ ...previous, source: undefined }));
                  resetTransientState();
                }}
                aria-invalid={errors.source ? true : undefined}
                className={cn(errors.source && "border-destructive focus-visible:ring-destructive")}
              />
              {errors.source && (
                <p className="text-sm text-destructive" role="alert">
                  {errors.source}
                </p>
              )}
               <p className="text-xs text-muted-foreground">
                 Twitch sources require an authorized source provider; if none is configured on this
                 deployment you will get a clear message, and direct upload always works.
               </p>
             </div>
           ) : sourceType === "video" ? (
             <div className="flex flex-col gap-2">
               <Label htmlFor="video-url">Video URL</Label>
               <Input
                 id="video-url"
                 type="url"
                 inputMode="url"
                 placeholder="https://storage.example.com/streams/vod.mp4"
                 value={videoUrl}
                 disabled={busy}
                 onChange={(event) => {
                   setVideoUrl(event.target.value);
                   setErrors((previous) => ({ ...previous, source: undefined }));
                   resetTransientState();
                 }}
                 aria-invalid={errors.source ? true : undefined}
                 className={cn(errors.source && "border-destructive focus-visible:ring-destructive")}
               />
               {errors.source && (
                 <p className="text-sm text-destructive" role="alert">
                   {errors.source}
                 </p>
               )}
               {(() => {
                 const kind = videoUrl.trim() ? classifyVideoUrl(videoUrl) : null;
                 if (!kind) return null;
                 const hint =
                   kind === "unrecognized"
                     ? {
                         tone: "warn" as const,
                         text: "Unrecognized link — paste a YouTube URL, a Twitch VOD/clip URL, or a direct link ending in .mp4, .webm or .mov.",
                       }
                     : VIDEO_TAB_HINTS[kind];
                 if (!hint) return null;
                 return (
                   <p
                     className={cn(
                       "text-xs",
                       hint.tone === "ok" ? "text-muted-foreground" : "text-amber-600 dark:text-amber-400"
                     )}
                   >
                     {hint.text}
                   </p>
                 );
               })()}
             </div>
           ) : (
            <div className="flex flex-col gap-4">
              <UploadArea
                file={file}
                disabled={busy}
                error={errors.source}
                onFileSelected={(next) => {
                  setFile(next);
                  setErrors((previous) => ({ ...previous, source: undefined }));
                  resetTransientState();
                }}
                onFileCleared={() => {
                  setFile(null);
                  setErrors((previous) => ({ ...previous, source: undefined }));
                  resetTransientState();
                }}
              />

              {uploadStage !== "idle" && createdJobId && (
                <div className="flex flex-col gap-2 rounded-md border bg-background/40 p-4">
                  <div className="flex items-center justify-between text-sm">
                    <span className="font-medium">
                      {uploadStage === "transferring" ? "Uploading to private storage…" : "Verifying upload…"}
                    </span>
                    <span className="tabular-nums text-muted-foreground">
                      {uploadStage === "transferring" ? `${uploadProgress}%` : "…"}
                    </span>
                  </div>
                  <Progress value={uploadStage === "transferring" ? uploadProgress : 100} />
                  <p className="text-xs text-muted-foreground">
                    {file ? `${file.name} (${formatBytes(file.size)})` : ""}
                    {" — transferred directly from your browser; nothing is proxied through the app server."}
                  </p>
                  {uploadStage === "transferring" && (
                    <Button type="button" variant="outline" size="sm" className="self-start" onClick={abortUpload}>
                      <Square size={14} /> Cancel upload
                    </Button>
                  )}
                </div>
              )}

              {canRetryUpload && (
                <div className="flex items-center gap-3">
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      if (createdJobId) void runUpload(createdJobId);
                    }}
                  >
                    <RefreshCw size={14} /> Retry upload
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    Job {createdJobId} is waiting for its file — retry re-requests a fresh authorization.
                  </p>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="card-surface h-fit animate-fade-in" style={{ animationDelay: "80ms" }}>
        <CardHeader>
          <CardTitle>Clip settings</CardTitle>
          <CardDescription>How many clips, and how long each one should be.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <Label htmlFor="clip-count">Number of clips</Label>
            <Select
              id="clip-count"
              value={clipCount}
              disabled={busy}
              onChange={(event) => setClipCount(Number(event.target.value))}
            >
              {COUNT_OPTIONS.map((count) => (
                <option key={count} value={count}>
                  {count} {count === 1 ? "clip" : "clips"}
                </option>
              ))}
            </Select>
            {errors.clipCount && (
              <p className="text-sm text-destructive" role="alert">
                {errors.clipCount}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="clip-duration">Clip duration</Label>
            <Select
              id="clip-duration"
              value={duration}
              disabled={busy}
              onChange={(event) => setDuration(Number(event.target.value))}
            >
              {DURATION_OPTIONS.map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds} seconds
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              Minimum {clips.minSeconds}s, maximum {clips.maxSeconds}s (each clip).
            </p>
            {errors.duration && (
              <p className="text-sm text-destructive" role="alert">
                {errors.duration}
              </p>
            )}
          </div>

          <Separator />

          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">Output (fixed by the product spec)</p>
            <div className="grid grid-cols-3 gap-2 text-center text-xs">
              <div className="rounded-md border bg-background/40 px-2 py-2">
                <p className="font-semibold text-foreground">{output.aspectRatio}</p>
                <p className="text-muted-foreground">Vertical</p>
              </div>
              <div className="rounded-md border bg-background/40 px-2 py-2">
                <p className="font-semibold text-foreground">
                  {output.width}x{output.height}
                </p>
                <p className="text-muted-foreground">Pixels</p>
              </div>
              <div className="rounded-md border bg-background/40 px-2 py-2">
                <p className="font-semibold text-foreground">{output.resolutionLabel}</p>
                <p className="text-muted-foreground">Resolution</p>
              </div>
            </div>
          </div>

          <Checkbox id="captions" checked={captionsEnabled} disabled={busy} onChange={(event) => setCaptionsEnabled(event.target.checked)}>
            <span className="text-sm">Modern animated subtitles</span>
          </Checkbox>

          {authRequired && (
            <div
              className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
              role="alert"
            >
              <Info size={16} className="mt-0.5 shrink-0" />
              <p>
                Your session is not active.{" "}
                <Link href="/access" className="font-medium underline underline-offset-2">
                  Enter your access code
                </Link>{" "}
                and come back.
              </p>
            </div>
          )}

          {serverError && !authRequired && (
            <p className="text-sm text-destructive" role="alert">
              {serverError}
            </p>
          )}

          {createdJobId && sourceType !== "upload" && (
            <div
              className="flex items-start gap-3 rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-accent-foreground"
              role="status"
            >
              <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-primary" />
              <p>Job created — opening the live processing view.</p>
            </div>
          )}

          <Button type="submit" size="lg" className="w-full" disabled={busy}>
            {busy ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                {uploadStage === "idle" ? "Working…" : "Uploading…"}
              </>
            ) : (
              <>
                <Wand2 size={16} /> Start Creating
              </>
            )}
          </Button>

          <p className="flex items-start gap-2 text-xs text-muted-foreground">
            <Info size={13} className="mt-0.5 shrink-0" />
            Uploads go directly to private storage and are verified server-side; the worker validates
            the media with FFprobe (Phase 3). AI transcription and clip detection arrive in Phase 4.
          </p>
        </CardContent>
      </Card>
    </form>
  );
}
