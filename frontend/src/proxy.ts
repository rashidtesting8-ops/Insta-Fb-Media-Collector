import { NextResponse, type NextRequest } from "next/server";
import {
  cookieName,
  dashboardOrigin,
  hasForwardingHeaders,
  isLoopbackHost,
  isLoopbackOrigin,
  validSession,
} from "./lib/security";
import { namedCall, namedCookieName } from "./lib/named";

export async function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname;
  const origin = dashboardOrigin();
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && (!origin || req.headers.get("origin") !== origin)) {
    return NextResponse.json({ error: "Origin denied" }, { status: 403 });
  }
  if (path === "/setup-admin" || path === "/api/auth/setup-admin") {
    if (!isLoopbackOrigin(origin) || !isLoopbackHost(req.headers.get("host")) || hasForwardingHeaders(req.headers)) {
      return NextResponse.json({ error: "Loopback access required" }, { status: 403 });
    }
    return NextResponse.next();
  }
  if (path.startsWith("/api/auth/named/") || ["/login", "/api/auth/login", "/api/ready"].includes(path) ||
      validSession(req.cookies.get(cookieName())?.value)) return NextResponse.next();
  const session = req.cookies.get(namedCookieName())?.value;
  if (session && await namedCall("session", {}, session).then(r => r.ok, () => false))
    return NextResponse.next();
  if (path.startsWith("/api/")) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  return NextResponse.redirect(new URL("/login", origin || req.url));
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
