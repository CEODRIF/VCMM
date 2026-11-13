"use client";

import * as React from "react";
import { CheckCircle2, Info, Wand2, Youtube } from "lucide-react";

import { appConfig } from "@/config/app";
import type { CreationOptions, SourceType, UploadedFileMeta } from "@/types";
import { cn } from "@/lib/utils";
import { validateClipCount, validateClipDuration, validateYoutubeUrl } from "@/lib/validation/clips";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { UploadArea } from "@/components/create/upload-area";

const { clips, output } = appConfig;

/** Duration options from min to max in step increments (60, 90, ... 180). */
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

/**
 * Main creation form.
 *
 * INTEGRATION POINT (Phase 2/3): on a valid submit this form will POST the
 * validated CreationOptions to /api/v1/jobs, which enqueues the worker.
 * Today it only validates and shows an explicit "not connected" state —
 * it never pretends processing happened.
 */
export function CreationForm() {
  const [sourceType, setSourceType] = React.useState<SourceType>("youtube");
  const [youtubeUrl, setYoutubeUrl] = React.useState("");
  const [file, setFile] = React.useState<UploadedFileMeta | null>(null);
  const [clipCount, setClipCount] = React.useState<number>(clips.defaultCount);
  const [duration, setDuration] = React.useState<number>(clips.defaultSeconds);
  const [captionsEnabled, setCaptionsEnabled] = React.useState(true);
  const [errors, setErrors] = React.useState<FieldErrors>({});
  const [submitted, setSubmitted] = React.useState(false);

  function clearSourceErrors() {
    setErrors((previous) => ({ ...previous, source: undefined }));
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitted(false);

    const nextErrors: FieldErrors = {};
    if (sourceType === "youtube") {
      const urlCheck = validateYoutubeUrl(youtubeUrl);
      if (!urlCheck.valid) nextErrors.source = urlCheck.error;
    } else if (!file) {
      nextErrors.source = "Drop a video file or pick one from your device.";
    }
    const countCheck = validateClipCount(clipCount);
    if (!countCheck.valid) nextErrors.clipCount = countCheck.error;
    const durationCheck = validateClipDuration(duration);
    if (!durationCheck.valid) nextErrors.duration = durationCheck.error;

    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) {
      setSubmitted(false);
      return;
    }

    const options: CreationOptions = {
      sourceType,
      youtubeUrl: sourceType === "youtube" ? youtubeUrl.trim() : undefined,
      uploadedFile: sourceType === "upload" ? file ?? undefined : undefined,
      clipCount,
      clipDurationSeconds: duration,
      outputAspect: "9:16",
      resolution: "1080p",
      captionsEnabled,
    };

    // INTEGRATION POINT (Phase 2/3): submit `options` to the job API here.
    console.debug("[ClipForge] validated creation options (Phase 1 - not submitted)", options);
    setSubmitted(true);
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="grid gap-6 lg:grid-cols-[1fr_340px]">
      <Card className="card-surface animate-fade-in">
        <CardHeader>
          <CardTitle>Source</CardTitle>
          <CardDescription>Paste a YouTube URL or upload a video file.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex gap-2" role="tablist" aria-label="Source type">
            {(
              [
                { value: "youtube", label: "YouTube URL" },
                { value: "upload", label: "Upload file" },
              ] as const
            ).map((tab) => (
              <button
                key={tab.value}
                type="button"
                role="tab"
                aria-selected={sourceType === tab.value}
                onClick={() => {
                  setSourceType(tab.value);
                  clearSourceErrors();
                  setSubmitted(false);
                }}
                className={cn(
                  "flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors",
                  sourceType === tab.value
                    ? "border-primary/50 bg-primary/15 text-accent-foreground"
                    : "border-input bg-background/40 text-muted-foreground hover:text-foreground"
                )}
              >
                <Youtube size={15} />
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
                onChange={(event) => {
                  setYoutubeUrl(event.target.value);
                  clearSourceErrors();
                  setSubmitted(false);
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
                In production the video is fetched through an authorized source provider, not the
                browser.
              </p>
            </div>
          ) : (
            <UploadArea
              file={file}
              error={errors.source}
              onFileSelected={(next) => {
                setFile(next);
                clearSourceErrors();
                setSubmitted(false);
              }}
              onFileCleared={() => {
                setFile(null);
                clearSourceErrors();
                setSubmitted(false);
              }}
            />
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
              onChange={(event) => {
                setClipCount(Number(event.target.value));
                setSubmitted(false);
              }}
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
              onChange={(event) => {
                setDuration(Number(event.target.value));
                setSubmitted(false);
              }}
            >
              {DURATION_OPTIONS.map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds} seconds
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              Minimum {clips.minSeconds}s, maximum {clips.maxSeconds}s.
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

          <Checkbox
            id="captions"
            checked={captionsEnabled}
            onChange={(event) => setCaptionsEnabled(event.target.checked)}
          >
            <span className="text-sm">Modern animated subtitles</span>
          </Checkbox>

          <Button type="submit" size="lg" className="w-full">
            <Wand2 size={16} /> Start Creating
          </Button>

          {submitted && (
            <div
              className="flex items-start gap-3 rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-accent-foreground"
              role="status"
            >
              <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-primary" />
              <p>
                Configuration validated. The processing backend is not connected yet, so no job was
                created. Job submission lands in Phase 2 (see docs/ROADMAP.md).
              </p>
            </div>
          )}

          <p className="flex items-start gap-2 text-xs text-muted-foreground">
            <Info size={13} className="mt-0.5 shrink-0" />
            Phase 1 foundation — video processing is intentionally not wired up.
          </p>
        </CardContent>
      </Card>
    </form>
  );
}
