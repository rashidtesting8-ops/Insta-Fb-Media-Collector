import { NextResponse } from "next/server";
import { admitRequest, BodyError, cookieName, cookieOptions, dashboardOrigin, issueSession, readObject } from "@/lib/security";

export async function POST(req: Request) {
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin()) {
    return NextResponse.json({ error: "Origin denied" }, { status: 403 });
  }
  if (!admitRequest("login")) {
    return NextResponse.json({ error: "Try again later" }, { status: 429, headers: { "Retry-After": "60" } });
  }
  try {
    const { password } = await readObject(req, 2048);
    const token = typeof password === "string" ? issueSession(password) : undefined;
    console.info(JSON.stringify({ event: "dashboard_login", success: !!token }));
    if (!token) return NextResponse.json({ error: "Invalid credentials" }, { status: 401 });
    const res = NextResponse.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
    res.cookies.set(cookieName(), token, cookieOptions());
    return res;
  } catch (error) {
    if (error instanceof BodyError) return NextResponse.json({ error: error.message }, { status: error.status });
    throw error;
  }
}
