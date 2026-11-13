import Link from "next/link";
import { ArrowRight, MonitorPlay } from "lucide-react";

import { appConfig } from "@/config/app";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";

export function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div className="glow-primary pointer-events-none absolute inset-0" aria-hidden />
      <div className="container relative flex flex-col items-center gap-8 py-24 text-center sm:py-32">
        <Badge variant="accent" className="animate-fade-in">
          {appConfig.output.aspectRatio} vertical clips · {appConfig.output.resolutionLabel}
        </Badge>

        <h1
          className="max-w-3xl animate-fade-in text-4xl font-bold tracking-tight sm:text-6xl"
          style={{ animationDelay: "60ms" }}
        >
          Turn long videos into short clips that{" "}
          <span className="bg-gradient-to-r from-primary to-accent-foreground bg-clip-text text-transparent">
            stop the scroll
          </span>
        </h1>

        <p className="max-w-xl animate-fade-in text-lg text-muted-foreground" style={{ animationDelay: "120ms" }}>
          {appConfig.description}
        </p>

        <div className="flex animate-fade-in flex-col items-center gap-3 sm:flex-row" style={{ animationDelay: "180ms" }}>
          <Link href="/access" className={buttonVariants({ size: "lg" })}>
            Get Started <ArrowRight size={16} />
          </Link>
          <a href="#how-it-works" className={buttonVariants({ size: "lg", variant: "outline" })}>
            <MonitorPlay size={16} /> See how it works
          </a>
        </div>

        <p className="animate-fade-in text-xs text-muted-foreground" style={{ animationDelay: "240ms" }}>
          No account needed — enter your access code and start creating.
        </p>
      </div>
    </section>
  );
}
