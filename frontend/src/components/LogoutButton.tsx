"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { LogoutIcon } from "@/components/Icons";

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
        "flex w-full items-center gap-2.5 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-left text-sm font-medium text-[#D7CFC5] transition hover:bg-white/10 hover:text-white disabled:opacity-50"
      }
    >
      <LogoutIcon className="h-4 w-4 shrink-0 text-[#9A9187]" />
      <span>{busy ? "Signing out…" : "Sign out"}</span>
    </button>
  );
}
