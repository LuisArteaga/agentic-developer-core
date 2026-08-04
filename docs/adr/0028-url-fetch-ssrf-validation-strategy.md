# ADR 0028: URL Fetch Worker Tool — Manual Per-Hop SSRF Validation

## Status
Accepted

## Context
The ReAct Worker (ADR-0011) gained a Web Search Worker Tool (#38 / ADR-0026) that lets it research inline during the Execute phase. Search runs server-side at OpenRouter, so it makes **no direct outbound HTTP to retrieved URLs** — that SSRF surface is deliberately confined to a separate **URL Fetch** Worker Tool (#40). URL Fetch retrieves the full text content of a specific URL (e.g. a documentation page surfaced by `web_search`) by issuing a direct outbound HTTP request from the orchestrator process.

The orchestrator runs inside a container that acts on an arbitrary Target Repository, and the URLs it fetches originate from model output (search results) — i.e. attacker-influenced. Server-Side Request Forgery (SSRF) is therefore a real, exploitable risk: a crafted URL could reach the cloud metadata endpoint (`169.254.169.254`), internal services, or loopback services. The tool must make direct outbound HTTP *and* neutralize this risk.

The naive pattern — resolve the hostname, check the IP, then fetch — has two well-documented gaps (confirmed by research; see References):

1. **TOCTOU gap**: `requests.get(url)` re-resolves DNS at request time, so the validated IP and the connected IP can differ (DNS rebinding).
2. **Redirect gap**: a public URL that returns `302 → http://169.254.169.254/` bypasses a check that only validated the start URL.

The issue (#40) constraints are explicit: port the proven `is_ssrf_safe_url()` / `is_url_allowed()` / `is_domain_allowed()` pattern from agentic-planner-core (ADR-0009 Radical Simplicity — do not invent a new SSRF implementation), block private/loopback/link-local/reserved/multicast/unspecified ranges, enforce optional strict-mode domain allowlisting from `config/sources.toml`, validate the final resolved IP *after redirects* (not just the initial hostname), and use a plain `User-Agent` (never the GitHub auth token).

## Decision
We implement the **URL Fetch** Worker Tool (`orchestrator/research_tools.py`) with a **manual redirect loop and per-hop resolved-IP validation**, porting planner-core's URL-level pattern and using stdlib `urllib` (per ADR-0017's "urllib over requests" minimal-dependency stance — `httpx`/`requests` are present only as transitive deps; the orchestrator already uses `urllib.request` in `nodes.py`).

Concretely:

- `is_ssrf_safe_url(url)` — rejects non-http/https schemes; resolves the hostname via `socket.getaddrinfo`; rejects the URL if **any** resolved address is `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast`, or `is_unspecified`. Resolving *all* addresses (not just the first) defends against DNS round-robin rebinding where a host resolves to both a public and a private address. Obfuscated IP forms (e.g. `http://2130706433/`) are normalized by `getaddrinfo` before the `ipaddress` check, so they cannot bypass validation (requires Python 3.11+ for the `ipaddress` octal-literal fix; this project requires `>=3.12`).
- `is_domain_allowed(url, sources)` — under `strict` mode, the URL must exactly match a `sources.urls` entry or its hostname must be an exact or subdomain match of a `sources.domains` entry. Non-strict bypasses the allowlist; SSRF protection always applies.
- `is_url_allowed(url, sources)` — `is_ssrf_safe_url(url) and is_domain_allowed(url, sources)`.
- `fetch_url` `@tool` — validates the start URL, then issues non-redirecting GETs (`follow_redirects` disabled via a custom `HTTPRedirectHandler` that raises to surface the `Location`). On each 3xx it re-validates the redirect target's resolved IP **and** strict-mode allowlist before following. The loop is bounded at `MAX_REDIRECTS = 5`. Responses are decoded with the declared charset (UTF-8 fallback, `errors="replace"`), truncated to 50,000 chars with a `[truncated due to length]` note, and binary content (null byte in the leading 1 KB) returns an error. Timeouts, HTTP errors, and URL errors return error strings — the Worker never crashes.
- The tool uses a plain `User-Agent` header, never the GitHub auth token (no credential leakage to third-party domains).
- The tool is added to `get_worker_tools()` (ADR-0011), and the Worker system prompt gains a section instructing the agent to use `fetch_url` *after* `web_search` when a search snippet is too short.
- The tool shares the cached `config/sources.toml` / `SourcesConfig` loader with Web Search (#38); it does not re-parse the config.

### The accepted residual trade-off
A per-hop check-then-fetch loop is **not** the gold-standard SSRF defense. The strongest pattern (drawbridge, httpx-secure) validates the IP *at TCP-connect time* and pins the connection to the validated IP, closing the per-hop TOCTOU gap entirely. We deliberately accept a residual per-hop TOCTOU: each hop validates all resolved IPs and then issues the request, leaving a small window where DNS could rebind between validation and connect. This is the inherited planner-core trade-off, chosen for Radical Simplicity and stdlib-only operation. The mitigation in depth is that the post-PR PR Review Judges (ADR-0014, ADR-0019) are the hard merge gate, and SSRF is a code-review concern surfaced in the Security judge's verdict.

## Considered Options

### 1. Transport-level IP pinning (drawbridge / httpx-secure pattern)
Rejected as the *implementation* here, but recorded as the known-stronger alternative. A custom `httpx` transport that resolves the IP, validates it, and rewrites the request to connect to the validated IP (setting `Host` / TLS SNI to the original hostname) closes the TOCTOU gap and re-validates on every redirect automatically. We rejected it because (a) it requires `httpx` as an explicit dependency, breaking ADR-0017's stdlib-`urllib` stance, (b) it invents a new SSRF implementation rather than porting planner-core's proven one (violating the issue's explicit constraint and ADR-0009), and (c) IP pinning with TLS SNI rewriting is non-trivial and fragile. It remains the documented "if we ever need to close the residual gap" upgrade path.

### 2. `follow_redirects=True` + validate only the start URL
Rejected. Fails the issue's explicit acceptance criterion ("SSRF check validates the final resolved IP after redirects, not just the initial hostname"). An open redirect on a public URL bouncing to `169.254.169.254` would bypass the check (the PraisonAI GHSA-qq9r-63f6-v542 advisory documents exactly this `web_crawl` SSRF).

### 3. Disable redirects entirely (no loop)
Rejected. Real documentation sites redirect (e.g. `docs.example.com/old` → `docs.example.com/new`, or http→https upgrades). A no-redirect tool would fail on legitimate fetches the Worker needs, defeating the tool's purpose. The manual loop gives redirect support *with* per-hop validation.

## Consequences

- **SSRF-neutralized for the documented threat model**: private/loopback/link-local/reserved/multicast/unspecified addresses, obfuscated IPs, DNS round-robin rebinding, and redirect-to-internal are all blocked. The cloud metadata endpoint (`169.254.169.254`) — the highest-value SSRF target — is rejected both as a start URL and as a redirect target.
- **Residual per-hop TOCTOU**: a determined attacker who can rebind DNS in the sub-millisecond window between `getaddrinfo` and the actual TCP connect, *and* who controls a fetched URL, could theoretically reach an internal address on a single hop. This is judged acceptable given the stdlib-only constraint, the planner-core inheritance mandate, and the layered merge gate.
- **Minimal dependency**: zero new dependencies; `urllib`, `socket`, `ipaddress`, `urllib.parse` are all stdlib.
- **Latency**: each fetch does a synchronous DNS resolution (and one per redirect hop). Acceptable — the Worker calls `fetch_url` infrequently, only when a search snippet is insufficient.
- **Shared config**: domain policy is consistent across `web_search` and `fetch_url` via the single cached `SourcesConfig`.

## Inspiration & References

- **tachyon-oss/drawbridge** (https://github.com/tachyon-oss/drawbridge): the gold-standard reference. Documents the TOCTOU gap ("there's a gap between check and use"), the DNS round-robin rebinding vector ("reject the entire request if *any* resolved IP is in a blocked range"), and the connect-time IP-pinning approach we *deliberately did not adopt* here (option 1).
- **Zaczero/httpx-secure** (https://github.com/Zaczero/httpx-secure): drop-in httpx SSRF protection via DNS caching + IP rewrite. Confirms the `ipaddress`-properties approach (`is_private`/`is_loopback`/`is_link_local`) and the "validate all resolved IPs" principle; reinforces the stdlib-`ipaddress` basis of our `is_ssrf_safe_url`.
- **chs.us — Python SSRF Prevention Guide** (https://chs.us/guides/ssrf): documents "resolve hostnames before validation", "re-validate after redirects", and the per-hop validation loop pattern we implement.
- **Stytch — Securing Identity APIs Against SSRF** (https://stytch.com/blog/securing-identity-apis-against-ssrf): documents obfuscated-IP attacks (`2130706433` for `127.0.0.1`), DNS rebinding, and the redirect-bypass vector — all of which `is_ssrf_safe_url`'s `getaddrinfo`-then-`ipaddress` check and the per-hop loop defend against.
- **PraisonAI advisory GHSA-qq9r-63f6-v542** (https://github.com/MervinPraison/PraisonAI/security/advisories/GHSA-qq9r-63f6-v542): a real SSRF in a `web_crawl` httpx fallback with `follow_redirects=True` and no validation — the exact failure mode our manual per-hop loop prevents.
- **CVE-2021-29921** (https://sick.codes/sick-2021-014): Python `ipaddress` octal-literal parsing bug fixed in 3.11+. This project requires `>=3.12`, so the stdlib `ipaddress` checks we rely on are safe.
- **agentic-planner-core** (`planner/tools/research.py`): the `is_ssrf_safe_url()` / `is_url_allowed()` / `is_domain_allowed()` / `fetch_url_content()` implementation this tool ports (ADR-0009 Radical Simplicity).
- **ADR-0009** (Radical Simplicity / pure-Python LangGraph), **ADR-0011** (prebuilt `create_react_agent` Worker Tool contract), and **ADR-0017** (minimal-dependency "urllib over requests" stance) govern the implementation posture.
