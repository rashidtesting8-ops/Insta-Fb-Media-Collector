export function workerUrl(path: string): string {
  const base = process.env.WORKER_URL || "http://127.0.0.1:8788";
  return `${base.replace(/\/$/, "")}${path}`;
}

export function n8nUrl(path: string): string {
  const base = process.env.N8N_URL || "http://127.0.0.1:5679";
  return `${base.replace(/\/$/, "")}${path}`;
}

export function workerHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (process.env.MEDIA_WORKER_TOKEN) h["X-API-Key"] = process.env.MEDIA_WORKER_TOKEN;
  return h;
}
