import type { Metadata } from "next";

import { CreationForm } from "@/components/create/creation-form";
import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = {
  title: "Create",
  description: "Turn a long video into short vertical clips.",
};

export default function CreatePage() {
  return (
    <div className="container py-10 sm:py-12">
      <PageHeader
        title="Create clips"
        description="Add a source, pick your clip settings, and start creating."
      />
      <CreationForm />
    </div>
  );
}
