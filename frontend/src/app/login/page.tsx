"use client";
import { useState } from "react";
import { BrandMark, FolderCloudIcon, ShieldCheckIcon, SparklesIcon } from "@/components/Icons";

export default function Login() {
  const [password, setPassword] = useState("");
  const [named, setNamed] = useState(true);
  const [name, setName] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(named ? "/api/auth/named/login" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(named ? { name: name.trim(), password } : { password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(typeof data?.error === "string" ? data.error : "Sign-in failed. Please check your credentials.");
      }
      // Full navigation clears Next.js App Router prefetch cache after HttpOnly auth cookie change.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-12">
      {/* Left luxury studio showcase */}
      <div className="relative hidden flex-col justify-between overflow-hidden bg-[#161412] p-12 text-[#FAF8F5] lg:col-span-5 lg:flex">
        <div
          className="pointer-events-none absolute -right-24 -top-24 h-80 w-80 rounded-full opacity-25 blur-3xl"
          style={{ background: "radial-gradient(circle, #0F766E 0%, transparent 70%)" }}
        />
        <div
          className="pointer-events-none absolute -bottom-24 -left-16 h-72 w-72 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #C8955A 0%, transparent 70%)" }}
        />

        <div className="relative flex items-center gap-3">
          <BrandMark className="h-10 w-10 shadow-lg" />
          <div>
            <div className="text-base font-semibold tracking-tight text-white">Atelier Media</div>
            <div className="text-[11px] font-medium tracking-[0.14em] text-[#C8955A]">PET GROOMING STUDIO AI</div>
          </div>
        </div>

        <div className="relative space-y-6">
          <h1 className="font-serif text-4xl font-medium leading-tight tracking-tight text-white">
            Curate authentic salon photography &amp; video, effortlessly.
          </h1>
          <p className="max-w-md text-sm leading-relaxed text-[#BEB5AA]">
            Collect 365 days of client website and Facebook media, let AI Visual Director V2 select the strongest
            assets, verify commercial rights, and deliver straight to private Google Drive.
          </p>

          <div className="space-y-3 pt-2">
            {[
              {
                Icon: SparklesIcon,
                title: "AI Visual Director V2",
                text: "Real pixel & video-frame inspection with hero rankings and crop guidance.",
              },
              {
                Icon: ShieldCheckIcon,
                title: "Two-Admin Safety & Rights Gate",
                text: "Independent review and explicit commercial rights tracking before any release.",
              },
              {
                Icon: FolderCloudIcon,
                title: "Private Google Drive Delivery",
                text: "Organized Best Images, Best Videos, and client Reports — zero public links.",
              },
            ].map(({ Icon, title, text }) => (
              <div key={title} className="flex items-start gap-3 rounded-xl border border-white/10 bg-white/[0.04] p-3.5">
                <Icon className="mt-0.5 h-5 w-5 shrink-0 text-[#5EEAD4]" />
                <div>
                  <div className="text-sm font-medium text-white">{title}</div>
                  <div className="mt-0.5 text-xs leading-relaxed text-[#A89F95]">{text}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="relative text-xs text-[#887F75]">
          Atelier Media Intelligence · Private Client Workspace
        </div>
      </div>

      {/* Right calm sign-in card */}
      <div className="flex flex-col items-center justify-center bg-[#FAF8F5] px-4 py-12 sm:px-8 lg:col-span-7">
        <div className="mb-8 flex items-center gap-3 lg:hidden">
          <BrandMark className="h-9 w-9" />
          <div>
            <div className="text-base font-semibold text-[#181614]">Atelier Media</div>
            <div className="text-[11px] font-medium tracking-wider text-[#9A6B37]">PET GROOMING STUDIO AI</div>
          </div>
        </div>

        <div className="studio-card w-full max-w-md p-7 sm:p-9">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight text-[#181614]">Welcome back</h2>
            <p className="mt-1.5 text-sm text-[#6B635B]">
              Sign in with your team account to manage client media collections and approvals.
            </p>
          </div>

          {/* Segmented switcher */}
          <div className="mt-6 grid grid-cols-2 rounded-xl border border-[#E7DFD5] bg-[#F5F0E8] p-1 text-sm">
            <button
              type="button"
              onClick={() => {
                setNamed(true);
                setPassword("");
                setError(null);
              }}
              className={`rounded-lg py-2 font-medium transition ${
                named ? "bg-white text-[#181614] shadow-sm" : "text-[#6B635B] hover:text-[#181614]"
              }`}
            >
              Named Admin
            </button>
            <button
              type="button"
              onClick={() => {
                setNamed(false);
                setPassword("");
                setError(null);
              }}
              className={`rounded-lg py-2 font-medium transition ${
                !named ? "bg-white text-[#181614] shadow-sm" : "text-[#6B635B] hover:text-[#181614]"
              }`}
            >
              Shared Access
            </button>
          </div>
          <p className="mt-2 text-xs text-[#7D746B]">
            {named
              ? "Recommended: required for submitting jobs, approving AI curation, and verifying rights."
              : "View-only / shared workspace sign-in without individual admin approval privileges."}
          </p>

          <form onSubmit={onSubmit} className="mt-6 space-y-4">
            {named && (
              <div>
                <label htmlFor="login-name" className="mb-1.5 block text-sm font-medium text-[#2C2824]">
                  Account name
                </label>
                <input
                  id="login-name"
                  required
                  autoComplete="username"
                  placeholder="e.g. ahsan-rashid"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="studio-input"
                />
              </div>
            )}

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label htmlFor="login-password" className="block text-sm font-medium text-[#2C2824]">
                  Password
                </label>
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="text-xs font-medium text-[#0F766E] hover:underline"
                >
                  {showPassword ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="login-password"
                type={showPassword ? "text" : "password"}
                required
                aria-label="Password"
                autoComplete="current-password"
                className="studio-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter your password"
              />
            </div>

            {error && (
              <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-sm text-rose-800">
                {error}
              </div>
            )}

            <button disabled={loading} className="studio-btn-primary mt-2 w-full py-2.5">
              {loading ? "Signing in…" : "Sign in to Studio"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
