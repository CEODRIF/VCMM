import Link from "next/link";

import { appConfig } from "@/config/app";

export function SiteFooter() {
  return (
    <footer className="mt-auto border-t border-border/60 bg-background/60">
      <div className="container flex flex-col items-center justify-between gap-4 py-8 text-center sm:flex-row sm:text-left">
        <p className="text-sm text-muted-foreground">
          {appConfig.name} — long-form video in, platform-ready vertical clips out.
        </p>
        <nav className="flex items-center gap-5 text-sm" aria-label="Footer">
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
