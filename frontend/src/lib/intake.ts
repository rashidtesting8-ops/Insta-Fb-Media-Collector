export async function submitIntake(body: Record<string, unknown>, base: string, token: string, send = fetch) {
  // Dashboard operators submit new jobs. Rights approval and resume use the trusted n8n interface.
  const fields = [
    "website_url", "instagram_url", "facebook_url", "business_name", "business_context",
    "lookback_days", "max_pages", "max_media", "max_social_files", "max_selected_images",
    "max_selected_videos", "user_selection_notes", "run_ai_curation",
    "social_permission_confirmed", "idempotency_key",
  ];
  const payload = Object.fromEntries(fields.filter((field) => Object.hasOwn(body, field)).map((field) => [field, body[field]]));
  const response = await send(new URL("/webhook/pipeline-intake", base), {
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
