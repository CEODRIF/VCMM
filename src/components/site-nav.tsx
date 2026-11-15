"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const NAV_LINKS = [
  { href: "/create", label: "Create" },
  { href: "/processing", label: "Processing" },
  { href: "/results", label: "Results" },
];

/**
 * TASK 016 — client-side nav links with an active-page indicator.
 * (The header shell itself stays a server component; only the links
 * need the router pathname.)
 */
export function SiteNav() {
  const pathname = usePathname();

  return (
    <nav className="flex items-center gap-1" aria-label="Main">
      {NAV_LINKS.map((link) => {
        const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "relative rounded-lg px-3 py-2 text-sm font-medium transition-all duration-200 ease-premium",
              active
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
            )}
          >
            {link.label}
            {active && (
              <span
                aria-hidden
                className="absolute inset-x-3 -bottom-px h-px bg-gradient-to-r from-transparent via-primary/70 to-transparent"
              />
            )}
          </Link>
        );
      })}
    </nav>
  );
}
