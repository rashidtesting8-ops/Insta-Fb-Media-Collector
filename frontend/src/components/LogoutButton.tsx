"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LogoutButton({ className = "" }: { className?: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function onLogout() {
    if (busy) return;
    setBusy(true);
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch {
      // Redirect to login even if session was already expired
    } finally {
      setBusy(false);
      router.push("/login");
    }
  }

  return (
    <button
      type="button"
      disabled={busy}
      onClick={onLogout}
      className={
        className ||
        "w-full rounded border border-slate-300 px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-50"
      }
    >
      {busy ? "Signing out…" : "Sign out"}
    </button>
  );
}
