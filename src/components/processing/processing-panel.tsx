import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Clapperboard,
  FileAudio,
  FlaskConical,
  Loader2,
  PackageCheck,
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
  uploading: {
    label: "Uploading",
    description: "Waiting for your video to be transferred to private storage.",
    icon: Upload,
  },
  queued: {
    label: "Queued",
    description: "Upload verified. Waiting for a worker to pick up the job.",
    icon: Loader2,
  },
  validating: {
    label: "Validating",
    description: "A worker is inspecting the media (FFprobe) and checking format, streams and duration.",
    icon: FlaskConical,
  },
  ready: {
    label: "Ready",
    description: "Ingestion complete. A worker will transcribe the audio and detect clips next.",
    icon: PackageCheck,
  },
  transcribing: {
    label: "Transcribing",
    description: "Extracting the audio track and running Whisper transcription with word-level timing.",
    icon: FileAudio,
  },
  analyzing: {
    label: "Analyzing",
    description: "Detecting complete-idea moments and scoring them for clip selection.",
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
 * Processing state UI (presentational) — TASK 016: live jobs only.
 *
 * The development placeholder job was removed: without a real
 * ?job=<id> the overview shows an honest empty state instead.
 */

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds % 60);
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

/**
 * TASK 015 P3 — the expected clip count for the job's generation mode.
 * full_coverage: ceil(sourceDuration / clipDuration) once the source has
 * been probed (null before ingestion); highlights: the requested upper bound.
 */
function expectedClipCount(job: Job): number | null {
  if (job.generationMode === "full_coverage") {
    if (job.sourceDurationSeconds != null && job.clipDurationSeconds) {
      return Math.ceil(job.sourceDurationSeconds / job.clipDurationSeconds);
    }
    return null;
  }
  return job.requestedClips;
}

function clipCountLabel(job: Job): string {
  if (job.generationMode === "full_coverage") {
    const count = expectedClipCount(job);
    return count != null
      ? `${count} clips expected (full video coverage)`
      : "full video coverage — clip count derived from the source duration";
  }
  if (job.requestedClips != null) {
    return `${job.requestedClips} ${job.requestedClips === 1 ? "clip" : "clips"} requested (viral highlights)`;
  }
  return "clips requested";
}

export function ProcessingPanel({ job }: { job: Job }) {
  const meta = STATE_META[job.status];
  const Icon = meta.icon;
  const stateIndex = appConfig.processingStates.indexOf(job.status);
  const expected = expectedClipCount(job);
  const detected = job.clipsCount ?? 0;
  const settingsRows: string[] = [];
  if (job.generationMode != null) {
    settingsRows.push(
      `Mode: ${job.generationMode === "full_coverage" ? "Full video to reels" : "Viral highlights"}`
    );
  }
  if (job.outputFps != null) {
    settingsRows.push(`Output frame rate: ${job.outputFps} FPS (explicit)`);
  }
  if (job.sourceDurationSeconds != null) {
    settingsRows.push(`Source duration: ${formatDuration(job.sourceDurationSeconds)}`);
  }
  if (expected != null) {
    settingsRows.push(
      job.generationMode === "full_coverage"
        ? `Expected clips: ${expected} (full coverage, ${job.clipDurationSeconds ?? "?"}s each)`
        : `Requested clips: ${expected} (upper bound)`
    );
  }
  if (detected > 0) {
    settingsRows.push(
      job.generationMode === "full_coverage" && expected != null
        ? `Cut so far: ${detected}/${expected} clips`
        : `Cut so far: ${detected} clip${detected === 1 ? "" : "s"}`
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
      <Card className="card-surface animate-fade-in">
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1.5">
             <CardTitle title={job.id}>Job {job.id.slice(0, 8)}…</CardTitle>
            <CardDescription>
              {job.sourceType === "youtube"
                ? "YouTube source"
                : job.sourceType === "twitch"
                  ? "Twitch source"
                  : job.sourceType === "direct_url"
                    ? "Imported video link"
                    : job.sourceType === "kick"
                      ? "Kick source"
                      : "Uploaded file"}{" "}
              · {clipCountLabel(job)}
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
            <p className="rounded-xl border border-destructive/40 bg-destructive/10 p-3.5 text-sm text-destructive" role="alert">
              {job.errorMessage}
            </p>
          )}

          {settingsRows.length > 0 && (
            <div className="flex flex-col gap-1 rounded-xl border bg-background/40 p-3.5 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Job settings</p>
              {settingsRows.map((row) => (
                <p key={row}>{row}</p>
              ))}
              {job.generationMode === "full_coverage" && (
                <p className="text-muted-foreground/80">
                  Every second of the source is covered in chronological order — no gaps, no skipped
                  sections. A final remainder shorter than 10s is folded into the last full clip.
                </p>
              )}
              {job.generationMode === "highlights" && (
                <p className="text-muted-foreground/80">
                  Clips are ranked by a transparent scoring model. A high score is an estimate of
                  engagement potential — not a guarantee of virality.
                </p>
              )}
            </div>
          )}

          <div className="flex items-start gap-2.5 rounded-xl border bg-background/40 p-3.5 text-xs text-muted-foreground">
            <span
              aria-hidden
              className={cn(
                "mt-0.5 h-2 w-2 shrink-0 rounded-full",
                job.status === "failed"
                  ? "bg-destructive"
                  : job.status === "completed"
                    ? "bg-emerald-400"
                    : "animate-pulse-soft bg-primary"
              )}
            />
            <p>
              <span className="font-medium text-foreground">Live job status</span> — fetched from
              your session and refreshed automatically. The worker progresses the job through the
              pipeline; nothing here is simulated.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card className="card-surface h-fit animate-fade-in" style={{ animationDelay: "80ms" }}>
        <CardHeader>
          <CardTitle className="text-base">Pipeline states</CardTitle>
          <CardDescription>All nine supported states (ingestion + product pipeline).</CardDescription>
        </CardHeader>
        <CardContent>
           <ol className="flex flex-col">
             {appConfig.processingStates.map((state, index) => {
               const stateMeta = STATE_META[state];
               const StateIcon = stateMeta.icon;
               const isCurrent = state === job.status;
               const isPast = index < stateIndex && job.status !== "failed";
               const isFailedHere = state === "failed" && job.status === "failed";
               return (
                 <li
                   key={state}
                   className={cn(
                     "relative flex items-center gap-3 py-2 pl-1 text-sm",
                     isCurrent && "rounded-xl bg-accent/70 px-2 -mx-1"
                   )}
                   aria-current={isCurrent ? "step" : undefined}
                 >
                   <span
                     className={cn(
                       "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border transition-colors duration-300",
                       isCurrent
                         ? "border-primary/60 bg-primary/15 text-primary shadow-lg shadow-primary/20"
                         : isPast
                           ? "border-border bg-secondary/60 text-foreground/60"
                           : isFailedHere
                             ? "border-destructive/50 bg-destructive/10 text-destructive"
                             : "border-border bg-background/40 text-muted-foreground/60"
                     )}
                   >
                     {isPast ? <Check size={14} strokeWidth={2.5} /> : <StateIcon size={14} className={cn(isCurrent && job.status === "queued" && "animate-spin")} />}
                   </span>
                   <span
                     className={cn(
                       isCurrent
                         ? "font-medium text-foreground"
                         : isPast
                           ? "text-foreground/60"
                           : isFailedHere
                             ? "font-medium text-destructive"
                             : "text-muted-foreground"
                     )}
                   >
                     {stateMeta.label}
                   </span>
                   {isCurrent && <Badge className="ml-auto rounded-full">current</Badge>}
                 </li>
               );
             })}
           </ol>
        </CardContent>
      </Card>
    </div>
  );
}
