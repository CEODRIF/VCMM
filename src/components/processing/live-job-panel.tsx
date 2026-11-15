"use client";

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, Film, KeyRound, RefreshCw } from "lucide-react";

import { ProcessingPanel } from "@/components/processing/processing-panel";
import { buttonVariants, Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { Job } from "@/types";

/**
 * Phase 4: polling stops on the final states (completed/failed) and when a
 * job is PARKED at 'analyzing' with its clips already persisted — analysis is
 * finished, rendering arrives in Phase 5, so the job will not change.
 * 'ready' is no longer a stop state: a running worker claims it within one
 * poll interval and moves it to 'transcribing'.
 */
const POLL_STOP_STATES: ReadonlySet<Job["status"]> = new Set(["completed", "failed"]);
const POLL_INTERVAL_MS = 2500;

function shouldStopPolling(job: Job): boolean {
  if (POLL_STOP_STATES.has(job.status)) return true;
  return job.status === "analyzing" && (job.clipsCount ?? 0) > 0;
}

interface PublicJob {
  id: string;
  sourceType: "youtube" | "upload";
  requestedClips: number | null;
  clipDurationSeconds: number;
  captionsEnabled: boolean;
  generationMode?: "highlights" | "full_coverage";
  outputFps?: number | null;
  sourceDurationSeconds?: number | null;
  status: Job["status"];
  progress: number;
  createdAt: string;
  completedAt?: string;
  enqueuedAt?: string | null;
  clipsCount?: number;
  errorMessage?: string;
}

/**
 * Live job status panel (Phase 2).
 *
 * Polls GET /api/v1/jobs/[id] — the server verifies the HttpOnly session
 * cookie and only returns jobs owned by this session. Polling stops once the
 * job reaches a terminal state (completed/failed).
 */
export function LiveJobPanel({ jobId }: { jobId: string }) {
  const [job, setJob] = React.useState<Job | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [authRequired, setAuthRequired] = React.useState(false);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function fetchOnce() {
      try {
        const response = await fetch(`/api/v1/jobs/${jobId}`, { cache: "no-store" });
        const data = (await response.json().catch(() => null)) as { job?: PublicJob; error?: string } | null;

        if (cancelled) return;

        if (response.status === 401) {
          setAuthRequired(true);
          setLoading(false);
          return;
        }
        if (response.status === 404) {
          setError("Job not found. It may have expired or belong to a different session.");
          setLoading(false);
          return;
        }
        if (!response.ok || !data?.job) {
          setError(data?.error ?? "Could not load the job status.");
          setLoading(false);
          return;
        }

        const publicJob = data.job;
        const mapped: Job = {
          id: publicJob.id,
          sourceType: publicJob.sourceType,
          requestedClips: publicJob.requestedClips,
          clipDurationSeconds: publicJob.clipDurationSeconds,
          generationMode: publicJob.generationMode,
          outputFps: publicJob.outputFps,
          sourceDurationSeconds: publicJob.sourceDurationSeconds,
          status: publicJob.status,
          progress: publicJob.progress,
          createdAt: publicJob.createdAt,
          completedAt: publicJob.completedAt,
          enqueuedAt: publicJob.enqueuedAt ?? undefined,
          clipsCount: publicJob.clipsCount,
          errorMessage: publicJob.errorMessage,
        };
        setJob(mapped);
        setLoading(false);

        if (!shouldStopPolling(mapped)) {
          timer = setTimeout(fetchOnce, POLL_INTERVAL_MS);
        }
      } catch {
        if (!cancelled) {
          setError("Network error while loading the job status.");
          setLoading(false);
        }
      }
    }

    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  if (loading) {
    // TASK 016 — skeleton (no layout shift, no fake job data).
    return (
      <div className="grid gap-6 lg:grid-cols-[1fr_320px]" aria-busy="true" aria-label="Loading job status">
        <Card className="card-surface">
          <CardContent className="flex flex-col gap-5 p-6">
            <div className="flex items-center justify-between">
              <Skeleton className="h-6 w-44" />
              <Skeleton className="h-6 w-24 rounded-full" />
            </div>
            <div className="flex items-center gap-4">
              <Skeleton className="h-12 w-12 rounded-xl" />
              <div className="flex-1">
                <Skeleton className="mb-2 h-4 w-32" />
                <Skeleton className="h-2 w-full rounded-full" />
                <Skeleton className="mt-3 h-3 w-3/4" />
              </div>
            </div>
            <Skeleton className="h-20 w-full rounded-xl" />
          </CardContent>
        </Card>
        <Card className="card-surface hidden lg:block">
          <CardContent className="flex flex-col gap-2 p-6">
            <Skeleton className="h-4 w-32" />
            {Array.from({ length: 7 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </CardContent>
        </Card>
      </div>
    );
  }

  if (authRequired) {
    return (
      <Card className="card-surface animate-fade-in">
        <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
          <span className="glass flex h-12 w-12 items-center justify-center rounded-2xl text-muted-foreground">
            <KeyRound size={20} strokeWidth={1.75} aria-hidden />
          </span>
          <p className="max-w-sm text-sm text-muted-foreground">
            Your session is not active, so this job cannot be shown.
          </p>
          <Link href="/access" className={buttonVariants()}>
            Enter your access code
          </Link>
        </CardContent>
      </Card>
    );
  }

  if (error || !job) {
    return (
      <Card className="card-surface animate-fade-in">
        <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl border border-destructive/40 bg-destructive/10 text-destructive">
            <AlertTriangle size={20} strokeWidth={1.75} aria-hidden />
          </span>
          <p className="max-w-sm text-sm text-muted-foreground">{error ?? "Job unavailable."}</p>
          <Link href="/processing" className={buttonVariants({ variant: "outline" })}>
            <RefreshCw size={14} aria-hidden /> Back to the overview
          </Link>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {job.status === "completed" && (
        <Link href={`/results?job=${job.id}`} className={buttonVariants({ size: "lg", className: "rounded-xl" })}>
          <Film size={16} aria-hidden /> View your clips
        </Link>
      )}
      <ProcessingPanel job={job} />
    </div>
  );
}
