import { NextResponse } from "next/server";
import { handleSetupStatus, handleSetupSubmit } from "@/lib/setup-admin";

export async function GET(req: Request) {
  const result = await handleSetupStatus(req);
  return NextResponse.json(result.body, { status: result.status, headers: result.headers });
}

export async function POST(req: Request) {
  const result = await handleSetupSubmit(req);
  return NextResponse.json(result.body, { status: result.status, headers: result.headers });
}
