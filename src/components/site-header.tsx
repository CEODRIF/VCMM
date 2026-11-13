import Link from "next/link";
import { Scissors } from "lucide-react";

import { appConfig } from "@/config/app";
import { Badge } from "@/components/ui/badge";

const NAV_LINKS = [
  { href: "/create", label: "Create" },
  { href: "/processing", label: "Processing" },
  { href: "/results", label: "Results" },
];

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/80 backdrop-blur-md">
      <div className="container flex h-16 items-center justify-between">
        <Link href="/" className="flex items-center gap-2.5" aria-label={`${appConfig.name} home`}>
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-lg shadow-primary/25">
            <Scissors size={18} />
          </span>
          <span className="text-base font-semibold tracking-tight">{appConfig.name}</span>
        </Link>

        <nav className="hidden items-center gap-1 sm:flex" aria-label="Main">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              {link.label}
            </Link>
          ))}
          <Badge variant="secondary" className="ml-2 hidden md:inline-flex">
            Phase 1
          </Badge>
        </nav>

        <Link
          href="/access"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90"
        >
          Get Started
        </Link>
      </div>
    </header>
  );
}
