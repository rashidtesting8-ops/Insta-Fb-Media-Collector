import { dashboardDenied } from "@/lib/server";
import { NextResponse } from "next/server";
import { cookieName, cookieOptions } from "@/lib/security";

export async function POST(req: Request) {
  const denied = await dashboardDenied(req);
  if (denied) return denied;
  const res = NextResponse.json({ ok: true });
  res.cookies.set(cookieName(), "", cookieOptions(0));
  return res;
}
