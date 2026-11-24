"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

export default function Login() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [named, setNamed] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(named ? "/api/auth/named/login" : "/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(named ? { name, password } : { password }),
      });
      if (!res.ok) throw new Error("Sign-in failed");
      router.push("/");
    } catch (err: unknown) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setLoading(false); }
  }

  return (
    <div className="mx-auto mt-20 max-w-sm rounded-lg border border-slate-200 bg-white p-6">
      <h1 className="mb-4 text-xl font-semibold">Sign in</h1>
      <form onSubmit={onSubmit} className="space-y-3">
        <label className="block text-sm">Sign-in method
          <select value={named ? "named" : "shared"} onChange={e => { setNamed(e.target.value === "named"); setPassword(""); setError(null); }}
            className="mt-1 block w-full rounded border p-2">
            <option value="shared">Shared dashboard</option><option value="named">Named Admin</option>
          </select></label>
        {named && <label className="block text-sm">Account name<input required autoComplete="username" value={name}
          onChange={e => setName(e.target.value)} className="mt-1 block w-full rounded border p-2" /></label>}
        <input type="password" required aria-label="Password" autoComplete="current-password" className="w-full rounded border border-slate-300 px-3 py-2 text-sm" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" />
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button disabled={loading} className="w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
