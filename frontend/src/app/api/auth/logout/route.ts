import { dashboardDenied } from "@/lib/server";
import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { namedCall, namedCookie, namedCookieOptions } from "@/lib/named";
import { cookieName, cookieOptions } from "@/lib/security";

export async function POST(req: Request) {
  const denied = await dashboardDenied(req);
  if (denied) return denied;
  const res = NextResponse.json({ ok: true });
  const named = (await cookies()).get(namedCookie)?.value;
  if (named) {
    const revoked = await namedCall("logout", {}, named);
    if (!revoked.ok && revoked.status !== 403) return NextResponse.json({ error: "Logout unavailable" }, { status: 503 });
    res.cookies.set(namedCookie, "", { ...namedCookieOptions, maxAge: 0 });
  }
  res.cookies.set(cookieName(), "", cookieOptions(0));
  return res;
}
