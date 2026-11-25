"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { BrandMark, ShieldCheckIcon } from "@/components/Icons";
import { MIN_SETUP_PASSWORD_LENGTH, SETUP_CSRF_HEADER, SETUP_CSRF_VALUE } from "@/lib/setup-admin";

type SetupState = "loading" | "available" | "unavailable" | "completed";

export default function SetupAdminPage() {
  const router = useRouter();
  const [state, setState] = useState<SetupState>("loading");
  const [setupToken, setSetupToken] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [showPasswords, setShowPasswords] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function loadStatus() {
      try {
        const res = await fetch("/api/auth/setup-admin", { cache: "no-store" });
        const data = await res.json().catch(() => ({}));
        if (cancelled) return;
        if (res.ok && data?.available === true) {
          setState("available");
        } else {
          setState("unavailable");
          if (typeof data?.error === "string" && res.status === 403) {
            setError(data.error);
          }
        }
      } catch {
        if (!cancelled) {
          setState("unavailable");
          setError("Setup service unavailable");
        }
      }
    }
    void loadStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const trimmedToken = setupToken.trim();
    const trimmedName = name.trim();
    if (!trimmedToken) {
      setError("One-time setup token is required.");
      return;
    }
    if (!trimmedName) {
      setError("Account name is required.");
      return;
    }
    if (password.length < MIN_SETUP_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_SETUP_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirmPassword) {
      setError("Password and confirmation do not match.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetch("/api/auth/setup-admin", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [SETUP_CSRF_HEADER]: SETUP_CSRF_VALUE,
        },
        body: JSON.stringify({
          setup_token: trimmedToken,
          name: trimmedName,
          password,
          confirm_password: confirmPassword,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 409) {
          setSetupToken("");
          setPassword("");
          setConfirmPassword("");
          setState("unavailable");
        }
        setError(typeof data?.error === "string" ? data.error : "Admin setup failed.");
        return;
      }
      setSetupToken("");
      setPassword("");
      setConfirmPassword("");
      setState("completed");
      router.replace("/login");
    } catch {
      setError("Unable to reach local setup endpoint.");
    } finally {
      setSubmitting(false);
    }
  }

  const passwordsMismatch = confirmPassword.length > 0 && password !== confirmPassword;
  const passwordTooShort = password.length > 0 && password.length < MIN_SETUP_PASSWORD_LENGTH;

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-[#FAF8F5] px-4 py-12">
      <div className="mb-7 flex items-center gap-3">
        <BrandMark className="h-10 w-10 shadow-md" />
        <div>
          <div className="text-base font-semibold tracking-tight text-[#181614]">Atelier Media</div>
          <div className="text-[11px] font-medium tracking-[0.14em] text-[#9A6B37]">INITIAL STUDIO SETUP</div>
        </div>
      </div>

      <div className="studio-card w-full max-w-md p-7 sm:p-8">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-teal-50 text-[#0F766E]">
            <ShieldCheckIcon className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-[#181614]">One-Time Named Admin Setup</h1>
            <p className="mt-1 text-xs leading-relaxed text-[#6B635B]">
              Localhost-only initial onboarding. Requires a single-use local setup token and permanently locks after creating Admin #1.
            </p>
          </div>
        </div>

        {state === "loading" && (
          <div className="mt-6 flex items-center gap-2 text-sm text-[#6B635B]">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[#0F766E]" />
            Checking setup availability…
          </div>
        )}

        {state === "unavailable" && (
          <div className="mt-6 space-y-4">
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3.5 text-sm leading-relaxed text-amber-950">
              {error || "Initial administrator setup is locked, already completed, or requires issuing a single-use local setup token."}
            </div>
            <Link href="/login" className="studio-btn-primary w-full py-2.5 text-center">
              Go to Sign in
            </Link>
          </div>
        )}

        {state === "completed" && (
          <div className="mt-6 space-y-4">
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3.5 text-sm text-emerald-900">
              Admin #1 created and bootstrap permanently locked. Redirecting to Sign in…
            </div>
            <Link href="/login" className="studio-btn-primary w-full py-2.5 text-center">
              Continue to Sign in
            </Link>
          </div>
        )}

        {state === "available" && (
          <form onSubmit={onSubmit} className="mt-6 space-y-4" autoComplete="off">
            <div>
              <div className="mb-1 flex items-center justify-between">
                <label htmlFor="setup-token" className="block text-sm font-medium text-[#2C2824]">
                  One-time setup token
                </label>
                <button
                  type="button"
                  onClick={() => setShowToken((v) => !v)}
                  className="text-xs font-medium text-[#0F766E] hover:underline"
                >
                  {showToken ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="setup-token"
                type={showToken ? "text" : "password"}
                required
                autoComplete="off"
                spellCheck={false}
                value={setupToken}
                onChange={(e) => setSetupToken(e.target.value)}
                placeholder="Paste single-use token from local CLI"
                className="studio-input"
              />
            </div>

            <div>
              <label htmlFor="admin-name" className="mb-1 block text-sm font-medium text-[#2C2824]">
                Account name
              </label>
              <input
                id="admin-name"
                type="text"
                required
                autoComplete="username"
                spellCheck={false}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. ahsan-rashid"
                className="studio-input"
              />
              <p className="mt-1 text-xs text-[#7D746B]">Allowed: letters, numbers, dot, underscore, @, hyphen.</p>
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <label htmlFor="new-password" className="block text-sm font-medium text-[#2C2824]">
                  New password
                </label>
                <button
                  type="button"
                  onClick={() => setShowPasswords((v) => !v)}
                  className="text-xs font-medium text-[#0F766E] hover:underline"
                >
                  {showPasswords ? "Hide passwords" : "Show passwords"}
                </button>
              </div>
              <input
                id="new-password"
                type={showPasswords ? "text" : "password"}
                required
                minLength={MIN_SETUP_PASSWORD_LENGTH}
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={`Minimum ${MIN_SETUP_PASSWORD_LENGTH} characters`}
                className="studio-input"
              />
              {passwordTooShort && (
                <p className="mt-1 text-xs text-amber-700">
                  Password must be at least {MIN_SETUP_PASSWORD_LENGTH} characters ({password.length}/{MIN_SETUP_PASSWORD_LENGTH}).
                </p>
              )}
            </div>

            <div>
              <label htmlFor="confirm-password" className="mb-1 block text-sm font-medium text-[#2C2824]">
                Confirm password
              </label>
              <input
                id="confirm-password"
                type={showPasswords ? "text" : "password"}
                required
                minLength={MIN_SETUP_PASSWORD_LENGTH}
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Re-enter password"
                className="studio-input"
              />
              {passwordsMismatch && <p className="mt-1 text-xs text-rose-600">Passwords do not match.</p>}
            </div>

            {error && (
              <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting || passwordTooShort || passwordsMismatch}
              className="studio-btn-primary w-full py-2.5"
            >
              {submitting ? "Creating account…" : "Create account"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
