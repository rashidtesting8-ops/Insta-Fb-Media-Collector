# Groq 403 / Cloudflare Error 1010 — Host vs Container Diagnostics

**Date:** 21 November 2026
**Mode:** Read-only diagnostics. No inference calls, no User-Agent spoofing, no proxy rotation, no TLS-verification changes, no model/provider switch, no Phase 6 work.
**Context:** Groq model `qwen/qwen3.8-27b` and key are configured; a bounded live call from the Docker worker was rejected with HTTP 403 / Cloudflare `error code: 1010`, while the same models API GET from the Windows host succeeds.

---

## 1. Most likely root cause (with evidence)

**Cloudflare's WAF/bot-signature rule rejects the specific HTTP client used by the worker (`urllib.request` from Python 3.12), before the Groq application layer.** It is **not** an IP/geo block, not DNS, not TLS trust, not a proxy, not a credential, and not the model.

Decisive evidence (all read-only, unauthenticated unless noted):

| Evidence | Result |
|---|---|
| Public egress IP — host vs container | **Both `202.47.34.94`** (Docker Desktop NAT). An IP-based ban would block both. |
| DNS for `api.groq.com` — host vs container | Identical Cloudflare records: A `104.18.38.236`, `172.64.149.20`; AAAA `2a06:98c1:3105::6812:26ec`, `2a06:98c1:310a::ac40:9514`. |
| TCP 443 from container | IPv4 **OK**; IPv6 unreachable (Docker bridge has no IPv6) — but the IPv4 path is the one used and it completes TLS. |
| TLS/CA from container | OpenSSL 3.5.7, `certifi` bundle + `/usr/lib/ssl/certs` present; TLS handshake completes (HTTP responses are received). Not a certificate-trust failure. |
| Proxy env | Host: `NO_PROXY` only; container: none. No proxy misconfiguration. |
| **Same container, different HTTP libraries** | `urllib.request` → **403, `error code: 1010`**; **`requests` 2.34.2 (default headers) → 401** (reached Groq's app, which correctly reports missing auth). Only the client signature changed. |
| Host (PowerShell/.NET) | 401 with `server: cloudflare`, `cf-ray: a5de89393c0fdcdb-KHI` — reached Groq's application. |
| Container (urllib) | 403 with `server: cloudflare`, `cf-ray: a5de8c83a84b3d3a-KHI` (unauth) and `a5de8c840b3da813-KHI` (auth models GET), body `error code: 1010`, 2026-11-21 09:08:16 UTC. |

Groq's documented error page lists 401/403/422/429 etc. as *application* responses with a structured JSON `{"error":{"message","type"}}` body. Our 403 has a bare `error code: 1010` body plus Cloudflare `cf-ray`/`server: cloudflare` headers and **no Groq error object** — confirming rejection **before** Groq's application processed the request.

## 2. Host-vs-container comparison

| Aspect | Windows host | Docker worker |
|---|---|---|
| Public IP | `202.47.34.94` | `202.47.34.94` (identical, via Docker NAT) |
| Proxy vars | `NO_PROXY=127.0.0.1,localhost,::1` | none |
| DNS A / AAAA | Cloudflare records above | identical |
| IPv6 egress | DNS records exist | unreachable (Docker bridge IPv4-only) |
| HTTP client | PowerShell 5.1 / .NET, `SystemDefault` TLS | Python 3.12.16 `urllib.request` (provider); `requests` 2.34.2 / urllib3 2.8.0 available |
| TLS stack | Windows Schannel | OpenSSL 3.5.7, certifi + system CA |
| Unauth GET result | **401** (app reached), CF ray `a5de…KHI` | **403 / 1010** with urllib; **401** with `requests` |
| Auth models GET | 200 (11 models) | 403 / 1010 with urllib (credential valid on host) |
| Docker network | — | `pgm-media_media-net`, bridge `172.20.0.0/16`; no proxy/DNS settings in Desktop config |

## 3. Exact recommended remediation

**Option A (recommended, legitimate client fix):** change only the provider's HTTP transport in `app/ai/provider.py::_http_post` from `urllib.request.urlopen` to `requests.post` (already installed; default headers, no User-Agent override), keeping the same URL, header dict (including `Authorization`), JSON body, `timeout_s`, JSON parsing and all existing retry/error handling. This is a normal library substitution — the same container, IP, credentials and model already pass with `requests`; it does **not** evade access controls, spoof a browser signature, or rotate proxies. Then:
1. re-run the 68 offline tests;
2. recreate **only** the worker (compose unchanged);
3. re-run the exact bounded 3-request live test (pixel probe → MODE A → MODE B) with the same $0.25 ceiling;
4. record genuine model id, token usage, estimated cost and non-mock `curation.json`.

No DNS, proxy, certificate, port or volume changes are indicated.

**Option B (if Option A still fails):** open a Groq support / Developer Community ticket using the captured evidence — endpoint `https://api.groq.com/openai/v1/models`, timestamps 2026-11-21 09:06–09:08 UTC, Cloudflare Ray IDs `a5de89393c0fdcdb-KHI` (host, 401), `a5de8c83a84b3d3a-KHI` (container unauth, 403), `a5de8c840b3da813-KHI` (container auth, 403), `a5de8d5d2c043d3a-KHI` (urllib compare, 403), and the note that `requests` from the same container receives 401. Ask whether their WAF can allowlist the account's legitimate client signature.

**Option C (NOT authorized here):** a host-side inference bridge accepting authenticated internal worker requests and using the same approved credentials. This changes the trust boundary and expands the attack surface; it requires a **separate security review and explicit approval** before any implementation. Not started.

## 4. Is provider support required?

**Not initially** — Option A is supported by direct evidence and is within our own code. Provider support becomes necessary only if Option A still returns 1010, in which case the Ray IDs above are ready. Groq's public error-code docs do not document a Cloudflare 1010 false-positive remedy; their support/community channels are the appropriate escalation path.

## 5. Security implications

- Same public IP on both sides means **no firewall/IP-allowlist change is justified** — do not widen network policy on a false premise.
- **Do not** spoof browser User-Agent/TLS signatures, rotate proxies, or retry inference repeatedly: those would breach the project's anti-bypass rules and the explicit diagnostic constraints.
- Keep `AI_API_KEY` only in local `.env` / n8n credentials; no key values appear in these diagnostics, logs, manifests or GitHub. All probes used read-only, unauthenticated requests except the previously attempted bounded inference call.
- Do **not** disable TLS verification: certificate trust was verified healthy; disabling it would create a real security regression.
- Option C would place a credential-bearing service on the host and open a new internal endpoint — a distinct security review item, not a default fix.

## 6. Safe implementation plan (pending your approval for Option A)

1. Edit `app/ai/provider.py::_http_post` only — swap `urllib.request` for `requests.post`; preserve timeout/headers/body/secret scrubbing/retry semantics.
2. Run `python -m pytest tests -q` in the container (expect 68/68).
3. Recreate only the worker (`docker compose -f compose.media.yml up -d --no-deps collector`).
4. Re-run one read-only `requests` models GET to confirm 401 (app reached) before spending any inference budget.
5. Re-run the **same 3-request bounded test**: pixel probe → MODE A → MODE B, shared tracker, $0.25 ceiling, no model/provider switch. If it fails for any reason, stop and report — do not relabel mock output as live.
6. Update `docs/PHASE_5_REPORT.md` §13 with real model id, usage, cost and results. If all checks pass, **then** request review of the PR containing the transport change; no direct push to `main`.

## 7. Readiness

**Blocked pending approval of Option A (or a provider-support decision).** Infrastructure is intact: worker + both n8n healthy, probe HTTP 200 `auth=verified`, all volumes/jobs/backups untouched; Docker Desktop was restarted only to enable container diagnostics. Uncommitted local changes from the earlier validation attempt (`compose.media.yml` AI forwarding, `provider.py` JSON-mode/token cap, `docs/PHASE_5_REPORT.md`) are preserved. No inference test was run during diagnostics and Phase 6 was not started.

**Stopping after this diagnostic report, as instructed.**

## Resolution note (21 November 2026)

Option A was approved and implemented: `urllib.request` → `requests.post` (default headers, no UA spoof, TLS verify ON, identical auth/body/timeout/retry/usage handling). From the same container, the unauthenticated models GET now returns **401 with `cf-ray`** instead of 403/1010 — the Cloudflare 1010 rejection is resolved. Live inference then reached Groq's application layer and was rejected for prompt-shape/model-image-limit reasons (400), not access control. No Ray ID escalation was needed. See `docs/PHASE_5_REPORT.md` §14.

