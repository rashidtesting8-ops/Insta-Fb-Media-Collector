/**
 * Translates raw network / HTTP / backend error messages into clear,
 * actionable plain-English explanations for non-technical studio operators.
 */
export function friendlyErrorMessage(raw: unknown, fallback = "Something went wrong. Please refresh and try again."): string {
  const text = (typeof raw === "string" ? raw : raw instanceof Error ? raw.message : "").trim();
  if (!text) return fallback;
  const low = text.toLowerCase();

  if (low.includes("failed to fetch") || low.includes("networkerror") || low.includes("load failed")) {
    return "Unable to reach the local Atelier Media server. Make sure the dashboard and Docker containers are running, then click Retry.";
  }
  if (low.includes("worker unavailable") || low.includes("media worker unavailable") || low.includes("n8n unavailable") || low.includes("delivery trigger unavailable")) {
    return "Local background services (Docker collector or n8n) are temporarily unreachable. Check Docker Desktop is running and try again.";
  }
  if (low.includes("401") || low.includes("sign-in required") || low.includes("session expired")) {
    return "Your sign-in session expired. Please sign in again to continue.";
  }
  if (low.includes("403") || low.includes("origin denied") || low.includes("independent") || low.includes("submitter")) {
    return text.length > 20 && !low.startsWith("http ")
      ? text
      : "This action requires a second Named Admin account (you cannot approve your own submission).";
  }
  if (low.includes("412") || low.includes("review changed") || low.includes("decision changed")) {
    return "This project was updated in another tab. Refresh the review panel and try again.";
  }
  if (low.includes("429") || low.includes("too many") || low.includes("busy")) {
    return "The studio worker is busy or rate-limited right now. Wait a few seconds and try again.";
  }
  if (low.includes("invalid delivery response") || low.includes("oauth") || low.includes("invalid_grant") || low.includes("unauthorized_client")) {
    return "Google Drive delivery could not complete via n8n. Open n8n (localhost:5679), verify the '06 - Google Drive Delivery' workflow is active and Google Drive OAuth is connected, then retry.";
  }
  if (low.includes("quota")) {
    return "Upstream API or Google Drive quota was reached. Check your API/Drive quota and retry shortly.";
  }
  if (low.includes("timed out") || low.includes("timeout")) {
    return `The request timed out before finishing (${text}). Your local files are safe — retry shortly.`;
  }
  if (/^http 5\d\d\b/i.test(text) || low === "502" || low === "503" || low === "504") {
    return "A local background service returned an error (HTTP 5xx). Confirm Docker containers (collector & n8n) are healthy and try again.";
  }
  return text;
}

/** Allow only safe http(s) URLs before binding to external <a href>. */
export function safeExternalHref(url: string | null | undefined, { httpsOnly = false } = {}): string | null {
  if (!url || typeof url !== "string") return null;
  try {
    const parsed = new URL(url);
    if (httpsOnly) return parsed.protocol === "https:" ? parsed.toString() : null;
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}
