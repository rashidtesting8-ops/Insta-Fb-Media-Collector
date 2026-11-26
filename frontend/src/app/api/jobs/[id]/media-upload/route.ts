import { cookies } from "next/headers";
import { namedBinaryCall, namedCookieName } from "@/lib/named";
import { dashboardOrigin } from "@/lib/security";

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const json = (data: unknown, status: number) =>
  Response.json(data, { status, headers: { "Cache-Control": "private, no-store" } });

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!/^[a-f0-9]{32}$/.test(id)) return json({ error: "Invalid job ID" }, 400);
  if (!dashboardOrigin() || req.headers.get("origin") !== dashboardOrigin())
    return json({ error: "Origin denied" }, 403);
  const session = (await cookies()).get(namedCookieName())?.value;
  if (!session) return json({ error: "Named Admin sign-in required to upload client media" }, 401);

  let formData: FormData;
  try {
    formData = await req.formData();
  } catch {
    return json({ error: "Expected multipart form upload" }, 400);
  }
  const file = formData.get("file");
  if (!(file instanceof File) || file.size <= 0) {
    return json({ error: "Select a non-empty ZIP archive, photo, or video file to upload." }, 400);
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return json({ error: "Upload exceeds the 100 MB limit." }, 413);
  }
  const rawDate = formData.get("published_date");
  const publishedDate = typeof rawDate === "string" && rawDate.trim() ? rawDate.trim() : null;
  if (publishedDate && !/^\d{4}-\d{2}-\d{2}$/.test(publishedDate)) {
    return json({ error: "Published date must use YYYY-MM-DD format." }, 400);
  }

  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const upstream = await namedBinaryCall(
      "collection-media-upload",
      {
        job_id: id,
        platform: "facebook",
        filename: (file.name || "facebook-media.zip").slice(0, 200),
        ...(publishedDate ? { published_date: publishedDate } : {}),
      },
      bytes,
      session,
      90_000,
    );
    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return json(
        { error: typeof data.detail === "string" ? data.detail : "Client media upload was rejected." },
        upstream.status,
      );
    }
    return json(data, upstream.status);
  } catch {
    return json({ error: "Client media upload service unavailable." }, 503);
  }
}
