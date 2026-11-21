import { Suspense } from "react";
import JobDetailClient from "./JobDetailClient";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Suspense fallback={<div className="p-4 text-sm text-slate-500">Loading…</div>}>
      <JobDetailClient id={id} />
    </Suspense>
  );
}
