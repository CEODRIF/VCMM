import {
  AlertTriangle,
  CheckCircle2,
  Clapperboard,
  FileAudio,
  Loader2,
  ScanSearch,
  Upload,
  type LucideIcon,
} from "lucide-react";

import { appConfig, type ProcessingState } from "@/config/app";
import type { Job } from "@/types";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

interface StateMeta {
  label: string;
  description: string;
  icon: LucideIcon;
}

const STATE_META: Record<ProcessingState, StateMeta> = {
  queued: {
    label: "Queued",
    description: "Waiting for a worker to pick up the job.",
    icon: Loader2,
  },
  uploading: {
    label: "Uploading",
    description: "Fetching the source video through the authorized provider.",
    icon: Upload,
  },
  transcribing: {
    label: "Transcribing",
    description: "Running Whisper over the audio track.",
    icon: FileAudio,
  },
  analyzing: {
    label: "Analyzing",
    description: "Scoring moments for clip selection.",
    icon: ScanSearch,
  },
  rendering: {
    label: "Rendering",
    description: "Cutting vertical 1080x1920 clips and burning subtitles.",
    icon: Clapperboard,
  },
  completed: {
    label: "Completed",
    description: "All clips are ready to download.",
    icon: CheckCircle2,
  },
  failed: {
    label: "Failed",
    description: "Something went wrong. Check the error message.",
    icon: AlertTriangle,
  },
};

/**
 * Processing state UI (presentational).
 *
 * Used for two kinds of data:
 *   - `live` = true: a real job fetched from /api/v1/jobs/[id] (Phase 2);
 *   - `live` = false: typed placeholder data for development (Phase 1).
 */
export function ProcessingPanel({ job, live = false }: { job: Job; live?: boolean }) {
  const meta = STATE_META[job.status];
  const Icon = meta.icon;
  const stateIndex = appConfig.processingStates.indexOf(job.status);

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
      <Card className="card-surface animate-fade-in">
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1.5">
            <CardTitle>Job {job.id}</CardTitle>
            <CardDescription>
              {job.sourceType === "youtube" ? "YouTube source" : "Uploaded file"} · {job.requestedClips}{" "}
              {job.requestedClips === 1 ? "clip requested" : "clips requested"}
            </CardDescription>
          </div>
          <Badge variant={job.status === "failed" ? "destructive" : job.status === "completed" ? "default" : "secondary"}>
            {STATE_META[job.status].label}
          </Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex items-center gap-4">
            <span
              className={cn(
                "flex h-12 w-12 shrink-0 items-center justify-center rounded-lg border",
                job.status === "failed"
                  ? "border-destructive/40 bg-destructive/10 text-destructive"
                  : "border-primary/30 bg-primary/10 text-primary"
              )}
            >
              <Icon size={22} className={cn(job.status === "queued" && "animate-spin")} />
            </span>
            <div className="flex-1">
              <div className="mb-2 flex items-baseline justify-between">
                <p className="text-sm font-medium">{meta.label}</p>
                <p className="text-sm tabular-nums text-muted-foreground">{job.progress}%</p>
              </div>
              <Progress value={job.progress} />
              <p className="mt-2 text-sm text-muted-foreground">{meta.description}</p>
            </div>
          </div>

          {job.status === "failed" && job.errorMessage && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
              {job.errorMessage}
            </p>
          )}

          {live ? (
            <div className="flex flex-col gap-1 rounded-md border bg-background/40 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Live job status</p>
              <p>
                Fetched from your session via GET /api/v1/jobs/{`[id]`}. The worker picks this job
                up in Phase 3 (queue) and progresses it through the pipeline in Phases 4-5.
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-1 rounded-md border bg-background/40 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Development placeholder</p>
              <p>
                This job is typed sample data for UI development. Create a real job from the
                Create page to see live status.
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="card-surface h-fit animate-fade-in" style={{ animationDelay: "80ms" }}>
        <CardHeader>
          <CardTitle className="text-base">Pipeline states</CardTitle>
          <CardDescription>All seven supported states.</CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-col gap-1">
            {appConfig.processingStates.map((state, index) => {
              const stateMeta = STATE_META[state];
              const StateIcon = stateMeta.icon;
              const isCurrent = state === job.status;
              const isPast = index < stateIndex && job.status !== "failed";
              const isFailedState = state === "failed";
              return (
                <li
                  key={state}
                  className={cn(
                    "flex items-center gap-3 rounded-md px-3 py-2 text-sm",
                    isCurrent && "bg-accent text-accent-foreground",
                    isPast && "text-foreground/70",
                    !isCurrent && !isPast && !isFailedState && "text-muted-foreground"
                  )}
                  aria-current={isCurrent ? "step" : undefined}
                >
                  <StateIcon size={16} className={cn(isCurrent && "text-primary")} />
                  <span className={cn(isCurrent && "font-medium")}>{stateMeta.label}</span>
                  {isCurrent && <Badge className="ml-auto">current</Badge>}
                </li>
              );
            })}
          </ol>
        </CardContent>
      </Card>
    </div>
  );
}
