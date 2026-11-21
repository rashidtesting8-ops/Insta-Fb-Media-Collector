import { n8nUrl, workerUrl } from "@/lib/server";
import { dashboardOrigin } from "@/lib/security";

export async function GET() {
  const secret = process.env.DASHBOARD_SESSION_SECRET;
  const password = process.env.DASHBOARD_PASSWORD;
  const configured = !!(dashboardOrigin() && password && secret && secret.length >= 32 && secret !== password &&
    process.env.MEDIA_WORKER_DASHBOARD_TOKEN && process.env.N8N_PIPELINE_TOKEN);
  const checks = await Promise.allSettled([workerUrl("/ready"), n8nUrl("/healthz/readiness")].map(
    (url) => fetch(url, { cache: "no-store", signal: AbortSignal.timeout(2500) }),
  ));
  const ok = configured && checks.every((check) => check.status === "fulfilled" && check.value.ok);
  return Response.json({ ok }, { status: ok ? 200 : 503 });
}
