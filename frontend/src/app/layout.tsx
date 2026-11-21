import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pet Grooming Media Intelligence",
  description: "Media collection, curation and delivery dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <div className="flex min-h-screen">
          <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white p-4 md:block">
            <div className="mb-6 text-lg font-semibold">Pet Grooming MI</div>
            <nav className="space-y-1 text-sm">
              {[
                ["/", "Dashboard"],
                ["/new", "New Collection"],
                ["/settings", "Settings"],
              ].map(([href, label]) => (
                <a key={href} href={href} className="block rounded px-3 py-2 hover:bg-slate-100">
                  {label}
                </a>
              ))}
            </nav>
          </aside>
          <main className="flex-1 p-4 md:p-8">{children}</main>
        </div>
      </body>
    </html>
  );
}
