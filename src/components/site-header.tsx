import Link from "next/link";
import { cookies } from "next/headers";
import { Scissors } from "lucide-react";

import { appConfig } from "@/config/app";
import { securityConfig } from "@/config/security";
import { SessionSignOut } from "@/components/session-sign-out";
import { SiteNav } from "@/components/site-nav";

/**
 * TASK 016 — floating glass application shell.
 *
 * A soft translucent bar (liquid glass) with the brand mark, the primary
 * navigation (client-side, with an active-page indicator) and the session
 * actions. Desktop: one row. Mobile: brand + actions on top, nav below —
 * no overlap, no clipping at 375px.
 */
export function SiteHeader() {
  return (
    <header className="sticky top-0 z-50 w-full px-4 pt-3 sm:px-6">
      <div className="glass container flex flex-col gap-2 rounded-2xl px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-5 sm:py-0 sm:h-14">
        <div className="flex items-center justify-between gap-2">
          <Link
            href="/"
            className="flex items-center gap-2.5 rounded-lg px-1 py-1 transition-opacity hover:opacity-90"
            aria-label={`${appConfig.name} home`}
          >
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-primary/70 text-primary-foreground shadow-lg shadow-primary/25">
              <Scissors size={15} strokeWidth={2.25} />
            </span>
            <span className="text-[15px] font-semibold tracking-tight">{appConfig.name}</span>
          </Link>

          <div className="flex items-center gap-1">
            {cookies().get(securityConfig.session.cookieName)?.value ? <SessionSignOut /> : null}
            <Link
              href="/access"
              className="inline-flex h-9 items-center justify-center rounded-xl bg-primary px-3 text-[13px] font-medium text-primary-foreground shadow-lg shadow-primary/20 transition-all duration-200 ease-premium hover:brightness-110 active:scale-[0.98] sm:px-4 sm:text-sm"
            >
              Get Started
            </Link>
          </div>
        </div>

        <SiteNav />
      </div>
    </header>
  );
}
