import type { Metadata } from "next";

import { ProcessingPanel } from "@/components/processing/processing-panel";
import type { Job } from "@/types";

export const metadata: Metadata = {
  title: "Processing",
  description: "Watch your video get transformed into clips.",
};

// DEVELOPMENT PLACEHOLDER DATA — typed sample job for the processing UI.
// Not fetched from any API; no real processing occurs in Phase 1.
const PLACEHOLDER_JOB: Job = {
  id: "dev-0001",
  sourceType: "youtube",
  sourceUrl: "https://youtube.com/watch?v=placeholder",
  requestedClips: 3,
  status: "rendering",
  progress: 64,
  createdAt: "2026-11-13T00:00:00.000Z",
};

export default function ProcessingPage() {
  return (
    <div className="container py-12">
      <div className="mb-8">
        <h1 className="text-3xl font-bold tracking-tight">Processing</h1>
        <p className="mt-2 text-muted-foreground">
          Your video moves through the pipeline, one stage at a time.
        </p>
      </div>
      <ProcessingPanel job={PLACEHOLDER_JOB} />
    </div>
  );
}
