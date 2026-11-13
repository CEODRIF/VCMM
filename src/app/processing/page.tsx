import type { Metadata } from "next";

import { LiveJobPanel } from "@/components/processing/live-job-panel";
import { ProcessingPanel } from "@/components/processing/processing-panel";
import type { Job } from "@/types";

export const metadata: Metadata = {
  title: "Processing",
  description: "Watch your video get transformed into clips.",
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// DEVELOPMENT PLACEHOLDER DATA — typed sample job for the overview view.
// Real jobs are shown via ?job=<id> (LiveJobPanel), which polls the API.
const PLACEHOLDER_JOB: Job = {
  id: "dev-0001",
  sourceType: "youtube",
  sourceUrl: "https://youtube.com/watch?v=placeholder",
  requestedClips: 3,
  status: "rendering",
  progress: 64,
  createdAt: "2026-11-13T00:00:00.000Z",
};

export default function ProcessingPage({
  searchParams,
}: {
  searchParams: { job?: string };
}) {
  const jobId = typeof searchParams.job === "string" && UUID_PATTERN.test(searchParams.job)
    ? searchParams.job
    : null;

  return (
    <div className="container py-12">
      <div className="mb-8">
        <h1 className="text-3xl font-bold tracking-tight">Processing</h1>
        <p className="mt-2 text-muted-foreground">
          {jobId
            ? "Live status for your job."
            : "Your video moves through the pipeline, one stage at a time."}
        </p>
      </div>
      {jobId ? <LiveJobPanel jobId={jobId} /> : <ProcessingPanel job={PLACEHOLDER_JOB} />}
    </div>
  );
}
