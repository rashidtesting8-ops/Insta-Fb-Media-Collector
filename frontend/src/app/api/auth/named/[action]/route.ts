import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { namedCall, namedCookieName, namedCookieOptionsFor } from "@/lib/named";
import { admitRequest, BodyError, dashboardOrigin, readObject } from "@/lib/security";

export async function POST(req: Request, ctx: { params: Promise<{ action: string }> }) {
  const { action } = await ctx.params;
  if (!["login", "logout", "create", "suspend", "role", "request-recovery"].includes(action))
    return NextResponse.json({ error: "Unknown action" }, { status: 404 });
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())
    return NextResponse.json({ error: "Origin denied" }, { status: 403 });
  if (action === "login" && !admitRequest("login"))
    return NextResponse.json({ error: "Login limit reached" }, { status: 429 });
  const cookieName = namedCookieName();
  const session = (await cookies()).get(cookieName)?.value;
  if (action !== "login" && !session)
    return NextResponse.json({ error: "Named login required" }, { status: 401 });
  try {
    const response = await namedCall(action, await readObject(req, 4096), session);
    if (!response.ok) return NextResponse.json({ error: "Named request rejected" }, { status: response.status });
    const data = await response.json();
    const result = NextResponse.json(action === "login" ? { ok: true } : data);
    if (action === "login") {
      if (typeof data.session !== "string" || !/^[\w-]{43}$/.test(data.session)) throw new Error();
      result.cookies.set(cookieName, data.session, namedCookieOptionsFor());
    }
    if (action === "logout") result.cookies.set(cookieName, "", namedCookieOptionsFor(0));
    return result;
  } catch (err) {
    return NextResponse.json({ error: "Named request unavailable" },
      { status: err instanceof BodyError ? err.status : 503 });
  }
}
