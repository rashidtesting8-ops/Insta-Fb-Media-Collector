"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import LogoutButton from "@/components/LogoutButton";
import { BrandMark, DashboardIcon, PlusCircleIcon, SettingsIcon, ShieldCheckIcon } from "@/components/Icons";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard", subtitle: "All client projects", Icon: DashboardIcon },
  { href: "/new", label: "New Collection", subtitle: "Start a 365-day scan", Icon: PlusCircleIcon },
  { href: "/settings", label: "Settings", subtitle: "Health & admin team", Icon: SettingsIcon },
] as const;

const STANDALONE_PATHS = ["/login", "/setup-admin"];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";
  const isStandalone = STANDALONE_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));

  if (isStandalone) {
    return <main className="min-h-screen w-full">{children}</main>;
  }

  return (
    <div className="flex min-h-screen flex-col bg-[#FAF8F5] text-[#181614] md:flex-row">
      {/* Mobile top bar */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-[#2A2622] bg-[#161412] px-4 py-3 text-[#FAF8F5] md:hidden">
        <Link href="/" className="flex items-center gap-2.5">
          <BrandMark className="h-8 w-8 shrink-0" />
          <div>
            <div className="text-sm font-semibold leading-tight tracking-tight">Atelier Media</div>
            <div className="text-[11px] text-[#B8AFA4]">Pet Grooming Studio</div>
          </div>
        </Link>
        <div className="flex items-center gap-2 text-xs">
          {NAV_ITEMS.map(({ href, label }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`rounded-lg px-2.5 py-1.5 font-medium transition ${
                  active ? "bg-[#0F766E] text-white" : "text-[#D7CFC5] hover:bg-white/10"
                }`}
              >
                {label === "New Collection" ? "New" : label}
              </Link>
            );
          })}
          <LogoutButton className="rounded-lg border border-white/15 px-2.5 py-1.5 text-xs text-[#D7CFC5] hover:bg-white/10" />
        </div>
      </header>

      {/* Desktop luxury studio sidebar */}
      <aside className="hidden w-68 shrink-0 flex-col justify-between border-r border-[#27231F] bg-[#161412] p-5 text-[#FAF8F5] md:sticky md:top-0 md:flex md:h-screen">
        <div>
          <Link href="/" className="group flex items-center gap-3 rounded-xl p-1.5 transition hover:bg-white/5">
            <BrandMark className="h-10 w-10 shrink-0 shadow-md" />
            <div>
              <div className="text-[15px] font-semibold leading-tight tracking-tight text-white">
                Atelier Media
              </div>
              <div className="mt-0.5 text-[11px] font-medium tracking-wide text-[#C8955A]">
                PET GROOMING STUDIO AI
              </div>
            </div>
          </Link>

          <div className="mt-6">
            <Link
              href="/new"
              className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-b from-[#0F766E] to-[#0C615A] px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:from-[#115E59] hover:to-[#0A4E49]"
            >
              <PlusCircleIcon className="h-4 w-4" />
              New Client Collection
            </Link>
          </div>

          <div className="mt-7 px-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-[#8E857B]">
            Workspace
          </div>
          <nav className="mt-2 space-y-1">
            {NAV_ITEMS.map(({ href, label, subtitle, Icon }) => {
              const active = href === "/" ? pathname === "/" || pathname.startsWith("/jobs/") : pathname.startsWith(href);
              return (
                <Link
                  key={href}
                  href={href}
                  className={`flex items-center gap-3 rounded-xl px-3 py-2.5 transition ${
                    active
                      ? "bg-white/12 text-white shadow-inner ring-1 ring-white/10"
                      : "text-[#CFC7BD] hover:bg-white/6 hover:text-white"
                  }`}
                >
                  <Icon className={`h-4 w-4 shrink-0 ${active ? "text-[#5EEAD4]" : "text-[#9A9187]"}`} />
                  <div className="min-w-0">
                    <div className="text-sm font-medium leading-tight">{label}</div>
                    <div className="truncate text-[11px] text-[#9A9187]">{subtitle}</div>
                  </div>
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="space-y-3">
          <div className="rounded-xl border border-white/10 bg-white/[0.04] p-3.5 text-xs">
            <div className="flex items-center gap-2 font-medium text-[#F5EFE8]">
              <ShieldCheckIcon className="h-4 w-4 text-[#5EEAD4]" />
              Human-Verified Delivery
            </div>
            <p className="mt-1.5 leading-relaxed text-[#A39A90]">
              Every client asset stays private until reviewed, rights-cleared, and explicitly released.
            </p>
          </div>
          <div className="border-t border-white/10 pt-3">
            <LogoutButton />
          </div>
        </div>
      </aside>

      {/* Main canvas — min-w-0 prevents wide pre/tables from clipping action controls */}
      <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 md:px-10 md:py-8">
        <div className="mx-auto max-w-6xl">{children}</div>
      </main>
    </div>
  );
}
