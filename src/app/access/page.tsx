import type { Metadata } from "next";

import { AccessForm } from "@/components/access/access-form";

export const metadata: Metadata = {
  title: "Access",
  description: "Enter your ClipForge AI access code.",
};

export default function AccessPage() {
  return (
    <div className="container flex min-h-[70vh] items-center justify-center py-16">
      <AccessForm />
    </div>
  );
}
