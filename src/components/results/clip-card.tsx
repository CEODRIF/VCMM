import { Download, Play } from "lucide-react";

import type { Clip } from "@/types";
import { formatDurationSeconds } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * Single clip result card.
 *
 * NONFUNCTIONAL (Phase 1): the preview, download button and storage path are
 * placeholders. Clip previews and downloads are wired up in Phase 6, after
 * the rendering pipeline exists (see docs/ROADMAP.md).
 */
export function ClipCard({ clip, index }: { clip: Clip; index: number }) {
  return (
    <div className="flex animate-fade-in flex-col overflow-hidden rounded-lg border bg-card" style={{ animationDelay: `${index * 60}ms` }}>
      {/* 9:16 preview placeholder */}
      <div className="relative flex aspect-[9/16] w-full items-center justify-center bg-gradient-to-br from-accent to-secondary">
        <span className="flex h-14 w-14 items-center justify-center rounded-full bg-background/60 text-foreground backdrop-blur-sm">
          <Play size={22} className="translate-x-0.5" />
        </span>
        <Badge className="absolute left-3 top-3">
          {formatDurationSeconds(clip.duration)}
        </Badge>
        <span className="absolute bottom-3 right-3 text-[10px] uppercase tracking-wide text-foreground/50">
          9:16 · 1080p
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Clip {clip.sortOrder + 1}
          </p>
          <h3 className="mt-1 font-medium leading-snug">{clip.title}</h3>
        </div>

        {/* NONFUNCTIONAL: download endpoint does not exist yet. */}
        <Button variant="outline" size="sm" className="mt-auto" disabled aria-disabled="true" title="Downloads arrive in Phase 6">
          <Download size={14} /> Download
        </Button>
      </div>
    </div>
  );
}
