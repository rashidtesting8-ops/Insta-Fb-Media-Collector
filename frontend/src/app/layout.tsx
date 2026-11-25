import type { Metadata } from "next";
import Link from "next/link";
import LogoutButton from "@/components/LogoutButton";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pet Grooming Media Intelligence",
  description: "Media collection, curation and delivery dashboard",
};

const NAV_ITEMS = [
  ["/", "Dashboard"],
  ["/new", "New Collection"],
  ["/settings", "Settings"],
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <div className="flex min-h-screen flex-col md:flex-row">
          <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 md:hidden">
            <div className="text-base font-semibold">Pet Grooming MI</div>
            <div className="flex items-center gap-3 text-sm">
              {NAV_ITEMS.map(([href, label]) => (
                <Link key={href} href={href} className="text-slate-700 hover:text-blue-600">
                  {label}
                </Link>
              ))}
              <LogoutButton className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100" />
            </div>
          </header>
          <aside className="hidden w-60 shrink-0 flex-col justify-between border-r border-slate-200 bg-white p-4 md:flex">
            <div>
              <div className="mb-6 text-lg font-semibold">Pet Grooming MI</div>
              <nav className="space-y-1 text-sm">
                {NAV_ITEMS.map(([href, label]) => (
                  <Link key={href} href={href} className="block rounded px-3 py-2 hover:bg-slate-100">
                    {label}
                  </Link>
                ))}
              </nav>
            </div>
            <div className="border-t border-slate-200 pt-4">
              <LogoutButton />
            </div>
          </aside>
          <main className="flex-1 p-4 md:p-8">{children}</main>
        </div>
      </body>
    </html>
  );
}
