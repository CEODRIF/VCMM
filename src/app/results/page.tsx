import type { Metadata } from "next";
import Link from "next/link";
import { Film } from "lucide-react";

import { ResultsPanel } from "@/components/results/results-panel";
import { PageHeader } from "@/components/page-header";
import { buttonVariants } from "@/components/ui/button";
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
 * explains where results come from (honest empty state — no placeholder
 * data).
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
    <div className="container py-10 sm:py-12">
      <PageHeader
        title="Results"
        description={
          jobId
            ? "Preview and download your generated clips."
            : "Your clips will appear here once a job completes."
        }
      />

      {jobId ? (
        <ResultsPanel jobId={jobId} />
      ) : (
        <Card className="card-surface animate-fade-in">
          <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
            <span className="glass flex h-14 w-14 items-center justify-center rounded-2xl text-muted-foreground">
              <Film size={24} strokeWidth={1.5} aria-hidden />
            </span>
            <div>
              <h2 className="text-base font-semibold tracking-tight">No results yet</h2>
              <p className="mx-auto mt-1.5 max-w-md text-sm leading-relaxed text-muted-foreground">
                When a video job finishes, open its results from the processing page to
                preview and download the clips.
              </p>
            </div>
            <Link href="/create" className={buttonVariants({ size: "lg" })}>
              <Film size={16} aria-hidden /> Create your first video
            </Link>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
