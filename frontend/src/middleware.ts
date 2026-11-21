import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PASSWORD = process.env.DASHBOARD_PASSWORD;

export function middleware(req: NextRequest) {
  if (!PASSWORD) return NextResponse.next();
  const { pathname } = req.nextUrl;
  if (pathname.startsWith("/api/auth")) return NextResponse.next();
  if (pathname === "/login") return NextResponse.next();
  const cookie = req.cookies.get("pgm_dash")?.value;
  if (cookie === PASSWORD) return NextResponse.next();
  if (pathname.startsWith("/api")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  return NextResponse.redirect(url);
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
