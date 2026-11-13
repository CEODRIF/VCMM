import { Archive, Captions, Film, Scissors, Sparkles, Youtube, type LucideIcon } from "lucide-react";

import { appConfig } from "@/config/app";
import { Card, CardContent } from "@/components/ui/card";

const ICONS: Record<string, LucideIcon> = {
  sparkles: Sparkles,
  film: Film,
  captions: Captions,
  youtube: Youtube,
  scissors: Scissors,
  archive: Archive,
};

export function Features() {
  return (
    <section id="how-it-works" className="container py-20">
      <div className="mx-auto mb-12 max-w-2xl text-center">
        <h2 className="text-3xl font-bold tracking-tight">Everything a short-form workflow needs</h2>
        <p className="mt-3 text-muted-foreground">
          A focused pipeline: add a source, let the AI find the moments, and download
          platform-ready clips.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {appConfig.features.map((feature) => {
          const Icon = ICONS[feature.icon] ?? Sparkles;
          return (
            <Card key={feature.title} className="card-surface transition-colors hover:border-primary/40">
              <CardContent className="flex flex-col gap-3 p-6">
                <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-accent text-accent-foreground">
                  <Icon size={18} />
                </span>
                <h3 className="font-semibold">{feature.title}</h3>
                <p className="text-sm text-muted-foreground">{feature.description}</p>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="mx-auto mt-16 grid max-w-3xl gap-4 sm:grid-cols-3">
        {[
          { step: "1", title: "Add a source", body: "Paste a YouTube URL or drop a video file." },
          { step: "2", title: "AI finds the moments", body: "Transcription and analysis pick the strongest clips." },
          { step: "3", title: "Download ready clips", body: "Vertical 1080p clips with animated subtitles." },
        ].map((item) => (
          <div key={item.step} className="flex flex-col gap-2 text-center sm:text-left">
            <span className="mx-auto flex h-8 w-8 items-center justify-center rounded-full bg-primary/15 text-sm font-semibold text-primary sm:mx-0">
              {item.step}
            </span>
            <h3 className="text-sm font-semibold">{item.title}</h3>
            <p className="text-sm text-muted-foreground">{item.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
