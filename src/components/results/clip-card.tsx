"use client";

import * as React from "react";
import { Download, Loader2, Play, RefreshCw, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatBytes, formatDurationSeconds } from "@/lib/utils";
import type { ResultsClipView } from "@/lib/results/view";

type PreviewState =
  | { status: "loading" }
  | { status: "ready"; url: string }
  | { status: "error" };

/** Source-timestamp label: 41s -> "00:41"; 75.5s -> "01:16". */
function sourceStamp(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/**
 * Single clip result card (Phase 6 — live; TASK 015 P4 extended).
 *
 * - Preview: a SHORT-LIVED signed URL is fetched lazily for this clip and
 *   played in a native <video> element. On failure the card degrades to a
 *   placeholder with a retry — the rest of the grid keeps working.
 * - Download: mints a short-lived signed URL (attachment flavour) and
 *   triggers the browser download through a transient anchor.
 * - Delete (P4): two-step inline confirmation (no fake optimistic state) —
 *   the card calls `onDeleted` ONLY after the server confirms the object is
 *   verified gone and the row is removed.
 * - Metadata (P4): every value is MEASURED server-side (ffprobe / storage
 *   listing); a missing value renders "—", never a guess.
 *
 * Both signed-URL endpoints and the delete endpoint re-verify session +
 * ownership + clip/job association on every request; this component never
 * sees storage paths or credentials.
 */
export function ClipCard({
  clip,
  jobId,
  index,
  onDeleted,
}: {
  clip: ResultsClipView;
  jobId: string;
  index: number;
  /** Called with the clip id after the SERVER confirms the permanent deletion. */
  onDeleted?: (clipId: string) => void;
}) {
  const [preview, setPreview] = React.useState<PreviewState>({ status: "loading" });
  const [downloading, setDownloading] = React.useState(false);
  const [downloadError, setDownloadError] = React.useState<string | null>(null);
  const [previewRetry, setPreviewRetry] = React.useState(0);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    setPreview({ status: "loading" });
    setDownloadError(null);

    if (!clip.downloadable) {
      setPreview({ status: "error" });
      return;
    }
    (async () => {
      try {
        const response = await fetch(`/api/v1/jobs/${jobId}/clips/${clip.id}/preview`, { cache: "no-store" });
        const data = (await response.json().catch(() => null)) as { url?: string } | null;
        if (cancelled) return;
        if (response.ok && data?.url) {
          setPreview({ status: "ready", url: data.url });
        } else {
          setPreview({ status: "error" });
        }
      } catch {
        if (!cancelled) setPreview({ status: "error" });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [jobId, clip.id, clip.downloadable, previewRetry]);

  // The inline confirmation auto-cancels (no stale "confirm" state).
  React.useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(false), 5000);
    return () => clearTimeout(t);
  }, [confirmDelete]);

  async function handleDownload() {
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await fetch(`/api/v1/jobs/${jobId}/clips/${clip.id}/download`, { cache: "no-store" });
      const data = (await response.json().catch(() => null)) as { url?: string; error?: string } | null;
      if (response.ok && data?.url) {
        // Cross-origin signed URL: the server sends Content-Disposition
        // attachment, so the browser downloads without navigating away.
        const anchor = document.createElement("a");
        anchor.href = data.url;
        anchor.target = "_blank";
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } else {
        setDownloadError(data?.error ?? "This clip is not available for download right now.");
      }
    } catch {
      setDownloadError("Network error while starting the download.");
    } finally {
      setDownloading(false);
    }
  }

  async function handleDelete() {
    // First click arms the confirmation; the second performs the deletion.
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      const response = await fetch(`/api/v1/jobs/${jobId}/clips/${clip.id}`, { method: "DELETE" });
      const data = (await response.json().catch(() => null)) as { deleted?: boolean; error?: string } | null;
      if (response.ok && data?.deleted) {
        // The server verified the object is gone and the row is removed.
        onDeleted?.(clip.id);
        return;
      }
      setDeleteError(
        data?.error ?? "Deletion did not complete — the clip is still available."
      );
      setConfirmDelete(false);
    } catch {
      setDeleteError("Network error during deletion — the clip is still available.");
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  }

  const startStamp = sourceStamp(clip.startTime);
  const endStamp = sourceStamp(clip.endTime);
  const resolution = clip.width && clip.height ? `${clip.width} × ${clip.height}` : null;
  const fps = clip.outputFps != null ? `${Math.round(clip.outputFps * 10) / 10} FPS` : null;

  return (
    <div
      className="flex animate-fade-in flex-col overflow-hidden rounded-lg border bg-card"
      style={{ animationDelay: `${index * 60}ms` }}
    >
      {/* 9:16 preview */}
      <div className="relative flex aspect-[9/16] w-full items-center justify-center bg-gradient-to-br from-accent to-secondary">
        {preview.status === "ready" && (
          <video
            src={preview.url}
            controls
            preload="metadata"
            playsInline
            className="h-full w-full bg-black object-contain"
            aria-label={`Clip preview: ${clip.title}`}
          />
        )}
        {preview.status === "loading" && clip.downloadable && (
          <span className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            <Loader2 size={16} className="animate-spin" aria-hidden /> Loading preview…
          </span>
        )}
        {preview.status === "error" && (
          <span className="flex flex-col items-center gap-2 text-sm text-muted-foreground">
            <Play size={18} aria-hidden />
            <span aria-hidden>{clip.downloadable ? "Preview unavailable" : "Not rendered"}</span>
            {clip.downloadable && (
              <button
                type="button"
                onClick={() => setPreviewRetry((k) => k + 1)}
                className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                <RefreshCw size={12} aria-hidden /> Retry
              </button>
            )}
          </span>
        )}
        <Badge className="absolute left-3 top-3">{formatDurationSeconds(clip.duration)}</Badge>
        <span className="absolute bottom-3 right-3 text-[10px] uppercase tracking-wide text-foreground/50">
          {resolution ? `${resolution} · 9:16` : "9:16"}
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Clip {clip.sortOrder + 1}
          </p>
          <h3 className="mt-1 font-medium leading-snug">{clip.title}</h3>
        </div>

        {/* Measured facts only — a missing value is "—", never a guess. */}
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <dt className="sr-only">Source time range</dt>
          <dd>{startStamp != null && endStamp != null ? `Source ${startStamp}–${endStamp}` : "Source —"}</dd>
          <dt className="sr-only">Generation mode</dt>
          <dd>{clip.mode === "full_coverage" ? "Full video" : clip.mode === "highlights" ? "Highlights" : "Mode —"}</dd>
          <dt className="sr-only">Output frame rate</dt>
          <dd>{fps ? `Rate ${fps}` : "Rate —"}</dd>
          <dt className="sr-only">File size</dt>
          <dd>{clip.fileSizeBytes != null ? `Size ${formatBytes(clip.fileSizeBytes)}` : "Size —"}</dd>
        </dl>

        <div className="mt-auto flex gap-2">
          <Button
            variant="outline"
            size="sm"
            className="flex-1"
            disabled={!clip.downloadable || downloading}
            onClick={handleDownload}
            aria-busy={downloading}
          >
            {downloading ? (
              <Loader2 size={14} className="animate-spin" aria-hidden />
            ) : (
              <Download size={14} aria-hidden />
            )}
            {downloading ? "Preparing…" : "Download"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="flex-1 border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
            disabled={deleting || !clip.downloadable}
            onClick={handleDelete}
            aria-busy={deleting}
          >
            {deleting ? (
              <Loader2 size={14} className="animate-spin" aria-hidden />
            ) : (
              <Trash2 size={14} aria-hidden />
            )}
            {deleting ? "Deleting…" : confirmDelete ? "Confirm delete" : "Delete"}
          </Button>
        </div>

        {confirmDelete && !deleting && (
          <p className="text-[11px] text-muted-foreground" role="status">
            This permanently removes the clip file. Confirm within a few seconds.
          </p>
        )}
        {deleteError && (
          <p className="text-xs text-destructive" role="alert">
            {deleteError}
          </p>
        )}
        {downloadError && (
          <p className="text-xs text-destructive" role="alert">
            {downloadError}
          </p>
        )}
      </div>
    </div>
  );
}
