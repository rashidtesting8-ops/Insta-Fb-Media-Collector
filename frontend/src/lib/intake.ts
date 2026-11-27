export const PROJECT_QA_INTAKE = "/webhook-test/phase10-exec-pipeline-intake";
const DEFAULT_INTAKE = "/webhook/pipeline-intake";
const socialHosts = {
  instagram_url: ["instagram.com", "www.instagram.com"],
  facebook_url: ["facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch"],
};

export function sourceUrlErrors(body: Record<string, unknown>) {
  const errors: Record<string, string> = {};
  for (const field of ["website_url", "instagram_url", "facebook_url"] as const) {
    const value = body[field];
    const optional = field === "instagram_url" || field === "facebook_url";
    if (value == null || (typeof value === "string" && !value.trim())) {
      if (!optional) {
        errors[field] = "URL is required";
      }
      continue;
    }
    const input = typeof value === "string" ? value.trim() : "";
    try {
      const authority = input.match(/^https?:\/\/([^/?#]+)/i)?.[1];
      if (!authority || input.length > 2048 || /[\x00-\x20\x7f\\]/.test(input) || /[@%]/.test(authority)) {
        throw new Error("Invalid URL");
      }
      const url = new URL(input);
      if (field === "website_url") {
        if (Object.values(socialHosts).flat().includes(url.hostname.replace(/\.$/, ""))) {
          errors[field] = "Use the business website URL";
        }
      } else if (url.protocol !== "https:" || !socialHosts[field].includes(url.hostname)) {
        errors[field] = "Use an HTTPS URL on the expected platform";
      }
    } catch {
      errors[field] = "Enter a valid HTTP(S) URL without credentials";
    }
  }
  return errors;
}

export async function submitIntake(body: Record<string, unknown>, base: string, token: string, send = fetch, path = DEFAULT_INTAKE) {
  if (path !== DEFAULT_INTAKE && path !== PROJECT_QA_INTAKE) {
    return { status: 503, body: { error: "Pipeline intake unavailable" } };
  }
  const upstream = new URL(base);
  if (path === PROJECT_QA_INTAKE && !["127.0.0.1", "localhost"].includes(upstream.hostname)) {
    return { status: 503, body: { error: "Pipeline intake unavailable" } };
  }
  const errors = Object.entries(sourceUrlErrors(body));
  if (errors.length) {
    return { status: 400, body: { error: errors.map(([field, message]) => field + ": " + message).join("; ") } };
  }
  // Dashboard operators submit new jobs. Rights approval and resume use the trusted n8n interface.
  const fields = [
    "website_url", "instagram_url", "facebook_url", "business_name", "business_context",
    "lookback_days", "max_pages", "max_media", "max_social_files", "max_selected_images",
    "max_selected_videos", "user_selection_notes", "run_ai_curation",
    "social_permission_confirmed", "idempotency_key",
  ];
  const payload = Object.fromEntries(fields.filter((field) => Object.hasOwn(body, field)).map((field) => [field, body[field]]));
  const response = await send(new URL(path, upstream), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Pipeline-Key": token },
    body: JSON.stringify(payload),
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(15_000),
  });
  const data = await response.json();
  if (response.status === 400 && Array.isArray(data?.errors)) {
    return { status: 400, body: { error: data.errors.join("; ") || "Invalid intake" } };
  }
  if (response.status !== 202 || typeof data?.job_id !== "string" || !/^[a-f0-9]{32}$/.test(data.job_id)) {
    return { status: 502, body: { error: "Pipeline intake unavailable" } };
  }
  return { status: 202, body: { job_id: data.job_id } };
}
