import type { Metadata } from "next";
import Link from "next/link";
import { Clapperboard, Film } from "lucide-react";

import { LiveJobPanel } from "@/components/processing/live-job-panel";
import { PageHeader } from "@/components/page-header";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "Processing",
  description: "Watch your video get transformed into clips.",
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * TASK 016 — the overview no longer shows a fake "dev-0001" job. Without a
 * ?job=<id> parameter there is simply no job to show: an honest empty state
 * with the next action (Create) instead of placeholder data.
 */
function EmptyJobState() {
  return (
    <Card className="card-surface animate-fade-in">
      <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
        <span className="glass flex h-14 w-14 items-center justify-center rounded-2xl text-muted-foreground">
          <Clapperboard size={24} strokeWidth={1.5} aria-hidden />
        </span>
        <div>
          <h2 className="text-base font-semibold tracking-tight">No job selected</h2>
          <p className="mx-auto mt-1.5 max-w-sm text-sm leading-relaxed text-muted-foreground">
            When a video job is created, its live status appears here — each pipeline
            stage, in real time, until the clips are ready.
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Link href="/create" className={buttonVariants({ size: "lg" })}>
            <Film size={16} aria-hidden /> Create your first video
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}

export default function ProcessingPage({
  searchParams,
}: {
  searchParams: { job?: string };
}) {
  const jobId =
    typeof searchParams.job === "string" && UUID_PATTERN.test(searchParams.job)
      ? searchParams.job
      : null;

  return (
    <div className="container py-10 sm:py-12">
      <PageHeader
        title="Processing"
        description={
          jobId
            ? "Live status for your job."
            : "Your video moves through the pipeline, one stage at a time."
        }
      />
      {jobId ? <LiveJobPanel jobId={jobId} /> : <EmptyJobState />}
    </div>
  );
}
