import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const pwd = process.env.DASHBOARD_PASSWORD;
  if (!pwd) return NextResponse.json({ ok: true });
  const body = await req.json().catch(() => ({}));
  if (body?.password !== pwd) return NextResponse.json({ error: "Invalid password" }, { status: 401 });
  const res = NextResponse.json({ ok: true });
  res.cookies.set("pgm_dash", pwd, { httpOnly: true, sameSite: "lax", path: "/" });
  return res;
}
