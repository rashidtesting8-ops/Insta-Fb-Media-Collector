"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h1 className="text-xl font-semibold text-slate-900">One-Time Named Admin Setup</h1>
      <p className="mt-1 text-xs text-slate-600">
        Loopback-only initial setup. Requires a single-use local setup token and permanently locks after creating Admin #1.
      </p>

      {state === "loading" && (
        <p className="mt-6 text-sm text-slate-600">Checking setup availability…</p>
      )}

      {state === "unavailable" && (
        <div className="mt-6 space-y-4">
          <div className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            {error || "Initial administrator setup is locked, already completed, or requires issuing a single-use local setup token."}
          </div>
          <Link
            href="/login"
            className="inline-block w-full rounded bg-blue-600 px-4 py-2 text-center text-sm font-medium text-white hover:bg-blue-700"
          >
            Go to Sign in
          </Link>
        </div>
      )}

      {state === "completed" && (
        <div className="mt-6 space-y-4">
          <div className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
            Admin #1 created and bootstrap permanently locked. Redirecting to Sign in…
          </div>
          <Link
            href="/login"
            className="inline-block w-full rounded bg-blue-600 px-4 py-2 text-center text-sm font-medium text-white hover:bg-blue-700"
          >
            Continue to Sign in
          </Link>
        </div>
      )}

      {state === "available" && (
        <form onSubmit={onSubmit} className="mt-5 space-y-4" autoComplete="off">
          <div>
            <div className="flex items-center justify-between">
              <label htmlFor="setup-token" className="block text-sm font-medium text-slate-800">
                One-time setup token
              </label>
              <button
                type="button"
                onClick={() => setShowToken((v) => !v)}
                className="text-xs text-blue-600 hover:underline"
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
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="admin-name" className="block text-sm font-medium text-slate-800">
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
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 text-sm"
            />
            <p className="mt-1 text-xs text-slate-500">Allowed characters: letters, numbers, dot, underscore, @, hyphen.</p>
          </div>

          <div>
            <div className="flex items-center justify-between">
              <label htmlFor="new-password" className="block text-sm font-medium text-slate-800">
                New password
              </label>
              <button
                type="button"
                onClick={() => setShowPasswords((v) => !v)}
                className="text-xs text-blue-600 hover:underline"
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
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 text-sm"
            />
            {passwordTooShort && (
              <p className="mt-1 text-xs text-amber-700">
                Password must be at least {MIN_SETUP_PASSWORD_LENGTH} characters ({password.length}/{MIN_SETUP_PASSWORD_LENGTH}).
              </p>
            )}
          </div>

          <div>
            <label htmlFor="confirm-password" className="block text-sm font-medium text-slate-800">
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
              className="mt-1 block w-full rounded border border-slate-300 px-3 py-2 text-sm"
            />
            {passwordsMismatch && (
              <p className="mt-1 text-xs text-red-600">Passwords do not match.</p>
            )}
          </div>

          {error && (
            <div role="alert" className="rounded border border-red-200 bg-red-50 p-2.5 text-sm text-red-700">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting || passwordTooShort || passwordsMismatch}
            className="w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? "Creating account…" : "Create account"}
          </button>
        </form>
      )}
    </div>
  );
}
