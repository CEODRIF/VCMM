import Link from "next/link";

import { appConfig } from "@/config/app";

export function SiteFooter() {
  return (
    <footer className="border-t bg-background">
      <div className="container flex flex-col items-center justify-between gap-4 py-8 sm:flex-row">
        <p className="text-sm text-muted-foreground">
          {appConfig.name} — Phase 1 foundation. Video processing is not connected yet.
        </p>
        <nav className="flex items-center gap-4 text-sm" aria-label="Footer">
          <Link href="/create" className="text-muted-foreground transition-colors hover:text-foreground">
            Create
          </Link>
          <Link href="/processing" className="text-muted-foreground transition-colors hover:text-foreground">
            Processing
          </Link>
          <Link href="/results" className="text-muted-foreground transition-colors hover:text-foreground">
            Results
          </Link>
        </nav>
      </div>
    </footer>
  );
}
