import type { Metadata } from "next";
import { FileArchive, Info } from "lucide-react";

import { ClipCard } from "@/components/results/clip-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Clip } from "@/types";

export const metadata: Metadata = {
  title: "Results",
  description: "Your generated clips.",
};

// DEVELOPMENT PLACEHOLDER DATA — typed sample clips for the results layout.
// The results API and clip storage arrive in Phase 6; nothing here is real.
const PLACEHOLDER_CLIPS: Clip[] = [
  {
    id: "dev-clip-1",
    jobId: "dev-0001",
    title: "The opening hook",
    startTime: 32,
    endTime: 92,
    duration: 60,
    sortOrder: 0,
    status: "ready",
  },
  {
    id: "dev-clip-2",
    jobId: "dev-0001",
    title: "The key insight",
    startTime: 540,
    endTime: 630,
    duration: 90,
    sortOrder: 1,
    status: "ready",
  },
  {
    id: "dev-clip-3",
    jobId: "dev-0001",
    title: "The payoff moment",
    startTime: 1180,
    endTime: 1270,
    duration: 90,
    sortOrder: 2,
    status: "ready",
  },
  {
    id: "dev-clip-4",
    jobId: "dev-0001",
    title: "The closing call to action",
    startTime: 1820,
    endTime: 1900,
    duration: 80,
    sortOrder: 3,
    status: "ready",
  },
];

export default function ResultsPage() {
  return (
    <div className="container py-12">
      <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Results</h1>
          <p className="mt-2 text-muted-foreground">
            Preview and download your generated clips.
          </p>
        </div>
        {/* NONFUNCTIONAL: ZIP packaging does not exist yet (Phase 6). */}
        <Button disabled aria-disabled="true" title="ZIP downloads arrive in Phase 6">
          <FileArchive size={16} /> Download all (ZIP)
        </Button>
      </div>

      <div className="mb-6 flex items-start gap-3 rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-accent-foreground" role="status">
        <Info size={16} className="mt-0.5 shrink-0 text-primary" />
        <p>
          Placeholder results for layout development. Clip generation, previews and downloads are
          not connected yet — see docs/ROADMAP.md (Phases 4-6).
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {PLACEHOLDER_CLIPS.map((clip, index) => (
          <ClipCard key={clip.id} clip={clip} index={index} />
        ))}
      </div>

      <Card className="card-surface mt-10">
        <CardHeader>
          <CardTitle className="text-base">What lands next</CardTitle>
          <CardDescription>
            The results page becomes live once jobs complete in the worker.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <ul className="list-disc space-y-1 pl-5">
            <li>Clip previews served from worker storage (Phase 5-6).</li>
            <li>Individual MP4 downloads and batch ZIP export (Phase 6).</li>
            <li>Automatic cleanup of expired jobs and clips (Phase 6).</li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
