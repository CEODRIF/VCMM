"use client";

import * as React from "react";
import Link from "next/link";
import { Loader2, RefreshCw } from "lucide-react";

import { ProcessingPanel } from "@/components/processing/processing-panel";
import { Card, CardContent } from "@/components/ui/card";
import type { Job } from "@/types";

/**
 * States where polling stops: completed/failed are final, and 'ready' is
 * terminal FOR INGESTION (Phase 3) — the product pipeline continues in
 * Phase 4, so we do not burn polls on a job that will not change.
 */
const POLL_STOP_STATES: ReadonlySet<Job["status"]> = new Set(["ready", "completed", "failed"]);
const POLL_INTERVAL_MS = 2500;

interface PublicJob {
  id: string;
  sourceType: "youtube" | "upload";
  requestedClips: number;
  clipDurationSeconds: number;
  captionsEnabled: boolean;
  status: Job["status"];
  progress: number;
  createdAt: string;
  completedAt?: string;
  enqueuedAt?: string | null;
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
          status: publicJob.status,
          progress: publicJob.progress,
          createdAt: publicJob.createdAt,
          completedAt: publicJob.completedAt,
          enqueuedAt: publicJob.enqueuedAt ?? undefined,
          errorMessage: publicJob.errorMessage,
        };
        setJob(mapped);
        setLoading(false);

        if (!POLL_STOP_STATES.has(mapped.status)) {
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
    return (
      <Card className="card-surface">
        <CardContent className="flex items-center justify-center gap-3 py-16 text-muted-foreground">
          <Loader2 size={18} className="animate-spin" /> Loading job status…
        </CardContent>
      </Card>
    );
  }

  if (authRequired) {
    return (
      <Card className="card-surface">
        <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
          <p className="text-sm text-muted-foreground">
            Your session is not active, so this job cannot be shown.
          </p>
          <Link
            href="/access"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
          >
            Enter your access code
          </Link>
        </CardContent>
      </Card>
    );
  }

  if (error || !job) {
    return (
      <Card className="card-surface">
        <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
          <p className="text-sm text-muted-foreground">{error ?? "Job unavailable."}</p>
          <Link
            href="/processing"
            className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline"
          >
            <RefreshCw size={14} /> Back to the overview
          </Link>
        </CardContent>
      </Card>
    );
  }

  return <ProcessingPanel job={job} live />;
}
