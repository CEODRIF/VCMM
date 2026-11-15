import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * TASK 016 — skeleton loader placeholder.
 *
 * Gentle pulse only (no layout shift, no bounce); respects
 * prefers-reduced-motion via the global kill-switch in globals.css
 * (animation is disabled, the block remains a quiet static surface).
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-md bg-muted/60", className)} {...props} />;
}

export { Skeleton };
