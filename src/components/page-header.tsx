import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * TASK 016 — consistent page header across all product routes.
 * One scale (title / lede / optional aside action), consistent width and
 * rhythm, so every page reads as the same product.
 */
export function PageHeader({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-8 flex flex-wrap items-end justify-between gap-4", className)}>
      <div className="min-w-0 max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">{title}</h1>
        {description && <p className="mt-1.5 text-[15px] leading-relaxed text-muted-foreground">{description}</p>}
      </div>
      {children ? <div className="flex shrink-0 items-center gap-2">{children}</div> : null}
    </div>
  );
}
