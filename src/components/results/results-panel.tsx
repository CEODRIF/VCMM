"use client";

import * as React from "react";
import Link from "next/link";
import {
  AlertTriangle,
  CalendarClock,
  FileArchive,
  Film,
  Loader2,
  RefreshCw,
  Trash2,
} from "lucide-react";

import { ClipCard } from "@/components/results/clip-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import {
  canCleanup,
  cleanupIncomplete,
  downloadableClips,
  interpretResults,
  isPurged,
  shouldPoll,
  type ResultsClipView,
  type ResultsJobView,
  type ResultsState,
  type ResultsZipView,
} from "@/lib/results/view";
import { formatBytes } from "@/lib/utils";

const POLL_INTERVAL_MS = 2500;

function CenterCard({ children }: { children: React.ReactNode }) {
  return (
    <Card className="card-surface">
      <CardContent className="flex flex-col items-center gap-4 py-12 text-center">{children}</CardContent>
    </Card>
  );
}

/**
 * Live results panel (Phase 6).
 *
 * Polls GET /api/v1/jobs/[id]/results (same session/ownership contract as
 * the job status endpoint) and renders the state machine decided by
 * `interpretResults` (src/lib/results/view.ts):
 *
 *   loading → processing (auto-polls until terminal)
 *          → ready (clip grid + ZIP) | failed | not-found | auth-required | error
 *
 * The expired sub-state keeps the grid visible but warns that the
 * artifacts are past their retention deadline.
 */
export function ResultsPanel({ jobId }: { jobId: string }) {
  const [state, setState] = React.useState<ResultsState>({ phase: "loading" });
  const [retry, setRetry] = React.useState(0);
  // TASK 015 P4: clips removed AFTER the server confirmed their deletion
  // (no optimistic updates — the server response is the trigger).
  const [removedIds, setRemovedIds] = React.useState<Set<string>>(new Set());
  const [cleanupBusy, setCleanupBusy] = React.useState(false);
  const [confirmCleanup, setConfirmCleanup] = React.useState(false);

  // The inline confirmation auto-cancels.
  React.useEffect(() => {
    if (!confirmCleanup) return;
    const t = setTimeout(() => setConfirmCleanup(false), 6000);
    return () => clearTimeout(t);
  }, [confirmCleanup]);

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function fetchOnce() {
      try {
        const response = await fetch(`/api/v1/jobs/${jobId}/results`, { cache: "no-store" });
        const data = (await response.json().catch(() => null)) as
          | { job?: ResultsJobView; clips?: ResultsClipView[]; zip?: ResultsZipView; error?: string }
          | null;
        if (cancelled) return;
        const next = interpretResults({ status: response.status, data }, new Date());
        setState(next);
        if (shouldPoll(next)) {
          timer = setTimeout(fetchOnce, POLL_INTERVAL_MS);
        }
      } catch {
        if (!cancelled) {
          setState({ phase: "error", message: "Network error while loading your results." });
        }
      }
    }

    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, retry]);

  function handleClipDeleted(clipId: string) {
    // Server-confirmed: hide the card and force a fresh results fetch.
    setRemovedIds((previous) => new Set(previous).add(clipId));
    setRetry((k) => k + 1);
  }

  async function handleCleanup() {
    // First click arms the confirmation; the second runs the cleanup.
    if (!confirmCleanup) {
      setConfirmCleanup(true);
      return;
    }
    setConfirmCleanup(false);
    setCleanupBusy(true);
    try {
      const response = await fetch(`/api/v1/jobs/${jobId}/cleanup`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      const data = (await response.json().catch(() => null)) as {
        purged?: boolean;
        errors?: string[];
        error?: string;
      } | null;
      if (response.ok && data && Array.isArray(data.errors)) {
        // Full or partial outcome — the results refetch shows the real
        // state (cleaned-up card, or the visible retryable failure banner).
        if (!data.purged && data.errors.length > 0) {
          // eslint-disable-next-line no-console
          console.warn("job cleanup partial failure:", data.errors);
        }
      } else {
        // eslint-disable-next-line no-console
        console.warn("job cleanup rejected:", response.status, data?.error ?? "");
      }
      setRetry((k) => k + 1);
    } catch {
      // eslint-disable-next-line no-console
      console.warn("job cleanup network error");
      setRetry((k) => k + 1);
    } finally {
      setCleanupBusy(false);
    }
  }

  if (state.phase === "loading") {
    return (
      <CenterCard>
        <Loader2 size={18} className="animate-spin text-muted-foreground" aria-hidden />
        <p className="text-sm text-muted-foreground" role="status">
          Loading your results…
        </p>
      </CenterCard>
    );
  }

  if (state.phase === "auth-required") {
    return (
      <CenterCard>
        <p className="text-sm text-muted-foreground">Your session is not active, so your results cannot be shown.</p>
        <Link
          href="/access"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
        >
          Enter your access code
        </Link>
      </CenterCard>
    );
  }

  if (state.phase === "not-found") {
    return (
      <CenterCard>
        <Film size={28} className="text-muted-foreground" aria-hidden />
        <p className="text-sm text-muted-foreground">
          Results not found. This job may have expired or belongs to a different session.
        </p>
        <Link
          href="/processing"
          className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline"
        >
          <RefreshCw size={14} aria-hidden /> Back to the overview
        </Link>
      </CenterCard>
    );
  }

  if (state.phase === "error") {
    return (
      <CenterCard>
        <AlertTriangle size={28} className="text-muted-foreground" aria-hidden />
        <p className="text-sm text-muted-foreground">{state.message}</p>
        <Button variant="outline" size="sm" onClick={() => setRetry((k) => k + 1)}>
          <RefreshCw size={14} aria-hidden /> Try again
        </Button>
      </CenterCard>
    );
  }

  if (state.phase === "processing") {
    const { data } = state;
    return (
      <CenterCard>
        <Loader2 size={28} className="animate-spin text-primary" aria-hidden />
        <div className="w-full max-w-sm">
          <div className="mb-2 flex items-center justify-between text-sm">
            <span className="font-medium">Your clips are being rendered</span>
            <span className="text-muted-foreground">{Math.round(data.job.progress)}%</span>
          </div>
          <Progress value={data.job.progress} />
        </div>
        <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
          This page refreshes automatically — or{" "}
          <Link href={`/processing?job=${data.job.id}`} className="font-medium text-primary hover:underline">
            follow the live status
          </Link>
          .
        </p>
      </CenterCard>
    );
  }

  if (state.phase === "failed") {
    return (
      <CenterCard>
        <AlertTriangle size={28} className="text-muted-foreground" aria-hidden />
        <div>
          <p className="text-sm font-medium">This job did not finish</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Something went wrong while processing your video.
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button variant="outline" size="sm" onClick={() => setRetry((k) => k + 1)}>
            <RefreshCw size={14} aria-hidden /> Try again
          </Button>
          <Link
            href="/"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
          >
            Start a new video
          </Link>
        </div>
      </CenterCard>
    );
  }

  // phase === "ready"
  const { data, expired } = state;
  const clips = downloadableClips(state).filter((c) => !removedIds.has(c.id));
  const zip = data.zip;
  const zipBlockedBySize = Boolean(zip && !zip.available && zip.totalBytes > 0);
  const purged = isPurged(state);
  const cleanupFailed = cleanupIncomplete(state);
  const modeLabel =
    data.job.generationMode === "full_coverage" ? "Full video to reels"
    : data.job.generationMode === "highlights" ? "Viral highlights"
    : null;

  return (
    <div className="space-y-6" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          {purged
            ? "All media for this job was permanently deleted."
            : clips.length > 0
              ? `${clips.length} clip${clips.length === 1 ? "" : "s"} ready — preview and download below.`
              : "The job finished, but no clips were rendered."}
          {modeLabel && !purged && <span aria-hidden> · {modeLabel}</span>}
          {data.job.outputFps != null && !purged && (
            <span aria-hidden> · {data.job.outputFps} FPS output</span>
          )}
        </p>
        <div className="flex flex-wrap items-center gap-2">
        {clips.length > 0 && !expired && !purged && (
          zip?.available ? (
            <a
              href={`/api/v1/jobs/${data.job.id}/download-all`}
              target="_blank"
              rel="noopener"
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
            >
              <FileArchive size={16} aria-hidden />
              Download all (ZIP){zip.totalBytes > 0 ? ` · ${formatBytes(zip.totalBytes)}` : ""}
            </a>
          ) : (
            <span className="inline-flex items-center gap-2 text-sm text-muted-foreground" title={zip ? `Limit: ${formatBytes(zip.limitBytes)}` : undefined}>
              <FileArchive size={16} aria-hidden />
              {zipBlockedBySize
                ? `All clips together (${zip ? formatBytes(zip.totalBytes) : "n/a"}) exceed the ZIP limit — download them individually.`
                : "No clips to package yet."}
            </span>
          )
        )}
        {canCleanup(state) && (
          <Button
            variant="outline"
            size="sm"
            className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
            disabled={cleanupBusy}
            onClick={handleCleanup}
            aria-busy={cleanupBusy}
          >
            {cleanupBusy ? (
              <Loader2 size={14} className="animate-spin" aria-hidden />
            ) : (
              <Trash2 size={14} aria-hidden />
            )}
            {cleanupBusy ? "Cleaning up…" : confirmCleanup ? "Confirm delete all" : "Delete all"}
          </Button>
        )}
        </div>
      </div>

      {confirmCleanup && !cleanupBusy && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
        >
          <Trash2 size={16} className="mt-0.5 shrink-0" aria-hidden />
          <p>
            This permanently deletes ALL generated clips and the source file for this job.
            Click “Confirm delete all” within a few seconds to proceed.
          </p>
        </div>
      )}

      {cleanupFailed && (
        <div
          role="alert"
          className="flex flex-wrap items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300"
        >
          <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden />
          <p className="min-w-0 flex-1">
            <span className="font-medium">Cleanup incomplete — retry available. </span>
            {data.job.cleanupError}
          </p>
          <Button variant="outline" size="sm" disabled={cleanupBusy} onClick={handleCleanup}>
            <RefreshCw size={14} aria-hidden /> Retry cleanup
          </Button>
        </div>
      )}

      {expired && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300"
        >
          <CalendarClock size={16} className="mt-0.5 shrink-0" aria-hidden />
          <p>
            These results are past their retention deadline. They may have already been cleaned up —
            if a download fails, the artifacts are no longer available.
          </p>
        </div>
      )}

      {clips.length === 0 ? (
        <CenterCard>
          <Film size={28} className="text-muted-foreground" aria-hidden />
          <p className="text-sm text-muted-foreground">
            {purged
              ? "All generated clips and the source file for this job have been permanently deleted."
              : "No clips are available to download for this job."}
          </p>
          <Link
            href="/"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
          >
            Try another video
          </Link>
        </CenterCard>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {clips.map((clip, index) => (
            <ClipCard
              key={clip.id}
              clip={clip}
              jobId={data.job.id}
              index={index}
              onDeleted={handleClipDeleted}
            />
          ))}
        </div>
      )}
    </div>
  );
}
