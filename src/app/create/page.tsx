import type { Metadata } from "next";

import { CreationForm } from "@/components/create/creation-form";

export const metadata: Metadata = {
  title: "Create",
  description: "Turn a long video into short vertical clips.",
};

export default function CreatePage() {
  return (
    <div className="container py-12">
      <div className="mb-8">
        <h1 className="text-3xl font-bold tracking-tight">Create clips</h1>
        <p className="mt-2 text-muted-foreground">
          Add a source, pick your clip settings, and start creating.
        </p>
      </div>
      <CreationForm />
    </div>
  );
}
