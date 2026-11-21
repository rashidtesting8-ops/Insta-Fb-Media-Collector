import { NextResponse, type NextRequest } from "next/server";
import { cookieName, dashboardOrigin, validSession } from "./lib/security";

export function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname;
  const origin = dashboardOrigin();
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && (!origin || req.headers.get("origin") !== origin)) {
    return NextResponse.json({ error: "Origin denied" }, { status: 403 });
  }
  if (["/login", "/api/auth/login", "/api/ready"].includes(path) ||
      validSession(req.cookies.get(cookieName())?.value)) return NextResponse.next();
  if (path.startsWith("/api/")) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  return NextResponse.redirect(new URL("/login", origin || req.url));
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
