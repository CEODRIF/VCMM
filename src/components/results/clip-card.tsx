"use client";

import * as React from "react";
import { Download, Loader2, Play, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatDurationSeconds } from "@/lib/utils";
import type { ResultsClipView } from "@/lib/results/view";

type PreviewState =
  | { status: "loading" }
  | { status: "ready"; url: string }
  | { status: "error" };

/**
 * Single clip result card (Phase 6 — live).
 *
 * - Preview: a SHORT-LIVED signed URL is fetched lazily for this clip and
 *   played in a native <video> element. On failure the card degrades to a
 *   placeholder with a retry — the rest of the grid keeps working.
 * - Download: mints a short-lived signed URL (attachment flavour) and
 *   triggers the browser download through a transient anchor.
 *
 * Both endpoints re-verify session + ownership + clip/job association on
 * every request; this component never sees storage paths or credentials.
 */
export function ClipCard({ clip, jobId, index }: { clip: ResultsClipView; jobId: string; index: number }) {
  const [preview, setPreview] = React.useState<PreviewState>({ status: "loading" });
  const [downloading, setDownloading] = React.useState(false);
  const [downloadError, setDownloadError] = React.useState<string | null>(null);
  const [previewRetry, setPreviewRetry] = React.useState(0);

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
          9:16 · 1080p
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Clip {clip.sortOrder + 1}
          </p>
          <h3 className="mt-1 font-medium leading-snug">{clip.title}</h3>
        </div>

        <Button
          variant="outline"
          size="sm"
          className="mt-auto"
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

        {downloadError && (
          <p className="text-xs text-destructive" role="alert">
            {downloadError}
          </p>
        )}
      </div>
    </div>
  );
}
