import type { Metadata } from "next";
import Link from "next/link";
import { Film } from "lucide-react";

import { ResultsPanel } from "@/components/results/results-panel";
import { Card, CardContent } from "@/components/ui/card";

export const metadata: Metadata = {
  title: "Results",
  description: "Your generated clips.",
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Results page (Phase 6 — live).
 *
 * `/results?job=<uuid>` shows the session's own job results (the server
 * re-verifies ownership on every request). Without a valid job id the page
 * explains where results come from instead of showing stale placeholder
 * data.
 */
export default function ResultsPage({
  searchParams,
}: {
  searchParams: { job?: string };
}) {
  const jobId =
    typeof searchParams.job === "string" && UUID_PATTERN.test(searchParams.job)
      ? searchParams.job
      : null;

  return (
    <div className="container py-12">
      <div className="mb-8">
        <h1 className="text-3xl font-bold tracking-tight">Results</h1>
        <p className="mt-2 text-muted-foreground">
          {jobId
            ? "Preview and download your generated clips."
            : "Your clips will appear here once a job completes."}
        </p>
      </div>

      {jobId ? (
        <ResultsPanel jobId={jobId} />
      ) : (
        <Card className="card-surface">
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <Film size={28} className="text-muted-foreground" aria-hidden />
            <p className="max-w-md text-sm text-muted-foreground">
              When a video job finishes, open its results from the processing
              page to preview and download the clips.
            </p>
            <Link
              href="/"
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
            >
              Create your first video
            </Link>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
