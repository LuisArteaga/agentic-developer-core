"""Web Search & URL Fetch Worker Tools.

Two on-demand Worker Tools (ADR-0011 ReAct pattern) for inline research during
the Execute phase, both sharing ``config/sources.toml`` (issues #38, #40):

* ``web_search`` — a structured-output wrapper around OpenRouter's server-side
  ``openrouter:web_search`` tool. Binds the tool to a cheap flash model
  (resolved via ``resolve_model_config("web_search")``), forces ``tool_choice``
  to guarantee search execution, instructs the model to append a JSON block of
  results, and parses it into ``[{title, url, snippet}]`` with snippets
  truncated to 300 chars. Search runs server-side at OpenRouter, so SSRF risk is
  near-zero (the tool makes no direct outbound HTTP to retrieved URLs — that is
  the URL Fetch tool's job). Ported from agentic-planner-core's
  ``planner/nodes/web_search.py`` and ``planner/tools/research.py``
  (ADR-0009 Radical Simplicity).

* ``fetch_url`` — retrieves the text content of a specific URL (e.g. a doc page
  found via web_search). Enforces SSRF protection via a manual redirect loop
  with per-hop resolved-IP validation (porting planner-core's
  ``is_ssrf_safe_url`` / ``is_url_allowed`` / ``is_domain_allowed``), and
  optional strict-mode domain allowlisting from ``sources.toml``. Uses stdlib
  ``urllib`` (not requests/httpx) per ADR-0017's minimal-dependency stance.
  See ADR-0028 for the SSRF validation strategy.
"""

import http.client
import ipaddress
import json
import logging
import re
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlsplit

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from orchestrator.config import get_chat_model_from_config, resolve_model_config
from orchestrator.sources_config import SourcesConfig, load_sources_config

logger = logging.getLogger("orchestrator.research_tools")

# Match planner-core: snippets are truncated to keep tool results concise and
# bound the tokens injected back into the Worker's ReAct context.
MAX_SNIPPET_CHARS = 300

_SYSTEM_INSTRUCTION = (
    "You are a technical researcher. You MUST use the openrouter:web_search tool "
    "to execute a search for the requested query. Do not try to answer without using the tool.\n\n"
    "After you perform the search and receive the results, synthesize your findings. "
    "At the end of your response, you MUST append a JSON block representing the list of "
    "search results you found. Snippets must be truncated to be concise "
    f"(max {MAX_SNIPPET_CHARS} chars per snippet).\n\n"
    "The JSON block must start with ```json and end with ```, and look exactly like this:\n"
    "[\n"
    "  {\n"
    '    "title": "Result Title",\n'
    '    "url": "http://example.com/result",\n'
    f'    "snippet": "A brief summary of findings (max {MAX_SNIPPET_CHARS} chars)"\n'
    "  }\n"
    "]"
)


def _build_search_tool_parameters(sources: SourcesConfig) -> dict[str, Any]:
    """Build the OpenRouter ``web_search`` tool ``parameters`` object from the
    loaded ``SourcesConfig``. Only includes parameters that are set, mirroring
    planner/nodes/web_search.py."""
    sp = sources.search
    params: dict[str, Any] = {"engine": sp.engine}
    if sp.search_context_size:
        params["search_context_size"] = sp.search_context_size
    if sp.max_results:
        params["max_results"] = sp.max_results
    if sp.max_total_results:
        params["max_total_results"] = sp.max_total_results
    # Strict mode restricts the search to configured domains.
    if sources.strict and sources.domains:
        params["allowed_domains"] = list(sources.domains)
    if sp.excluded_domains:
        params["excluded_domains"] = list(sp.excluded_domains)
    return params


def _extract_json_block(text: str) -> str:
    """Extract a JSON array block from Markdown output.

    Prefers a fenced ```json block; falls back to the first ``[...]`` array
    pattern. Ported verbatim from planner/nodes/web_search.py.
    """
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\[.*\])", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _coerce_text(content: Any) -> str:
    """Coerce an LLM response ``content`` (str or list of content blocks) to a
    single string for JSON extraction."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content) if content is not None else ""


def _parse_search_results(text_content: str) -> list[dict[str, str]]:
    """Parse the model's structured JSON output into ``[{title, url, snippet}]``,
    truncating snippets to ``MAX_SNIPPET_CHARS``. Returns ``[]`` on parse
    failure or when the output is not a list of URL-bearing objects."""
    results: list[dict[str, str]] = []
    try:
        json_text = _extract_json_block(text_content)
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse web_search JSON output: %s", e)
        return []

    if not isinstance(parsed, list):
        logger.warning("web_search JSON output was not a list: %r", type(parsed))
        return []

    for item in parsed:
        if isinstance(item, dict) and "url" in item:
            snippet = item.get("snippet", "")
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[: MAX_SNIPPET_CHARS - 3] + "..."
            results.append(
                {
                    "title": item.get("title", "No Title"),
                    "url": item.get("url", ""),
                    "snippet": snippet,
                }
            )
    return results


@tool
def web_search(query: str) -> str:
    """Search the web for current information.

    Use this tool when you encounter an unfamiliar API, a library version change,
    or an error message you cannot resolve from the codebase alone. Returns a JSON
    list of results: [{"title": str, "url": str, "snippet": str}].
    """
    sources = load_sources_config()
    cfg = resolve_model_config("web_search")
    logger.info(
        "Running web_search for query: %s (model=%s, engine=%s, strict=%s)",
        query,
        cfg["model"],
        sources.search.engine,
        sources.strict,
    )
    llm = get_chat_model_from_config(cfg)

    tool_definition: dict[str, Any] = {
        "type": "openrouter:web_search",
        "parameters": _build_search_tool_parameters(sources),
    }
    # Bind the server-side tool and force tool_choice to guarantee the model
    # actually executes the search rather than answering from memory.
    llm_with_tools = llm.bind(
        tools=[tool_definition],
        tool_choice={"type": "openrouter:web_search"},
    )

    user_message = f"Search Query: {query}"
    if sources.strict and sources.domains:
        user_message += (
            "\nAllowed Domains (search is strictly restricted to these): "
            f"{', '.join(sources.domains)}"
        )

    try:
        response = llm_with_tools.invoke(
            [
                SystemMessage(content=_SYSTEM_INSTRUCTION),
                HumanMessage(content=user_message),
            ]
        )
    except Exception as e:
        logger.error("web_search invocation failed: %s", e)
        return f"Error executing web_search for query '{query}': {e}"

    text_content = _coerce_text(response.content)
    results = _parse_search_results(text_content)
    if not results:
        return f"No search results found for query: {query}"
    return json.dumps(results, ensure_ascii=False)


# ---------------------------------------------------------------------------
# URL Fetch Worker Tool (issue #40) — SSRF-safe retrieval of a URL's text.
#
# SSRF protection is implemented as a manual redirect loop with per-hop
# resolved-IP validation, porting planner-core's URL-level pattern. See
# ADR-0028 for the rationale (manual per-hop vs. transport-level IP pinning)
# and the external references.
# ---------------------------------------------------------------------------

# Schemes permitted by fetch_url. file://, gopher://, ftp://, etc. are SSRF
# vectors and are rejected.
ALLOWED_SCHEMES = {"http", "https"}

# Response truncation bound (matches planner-core's fetch_url_content).
MAX_FETCH_CHARS = 50_000
# Byte read budget: large enough to decode up to MAX_FETCH_CHARS of 4-byte
# UTF-8 and still detect whether the body exceeds the cap.
MAX_FETCH_BYTES = (MAX_FETCH_CHARS * 4) + 1
TRUNCATION_NOTE = "\n[truncated due to length]"

# Per-hop and overall fetch budgets.
FETCH_TIMEOUT = 15.0
MAX_REDIRECTS = 5

# Plain User-Agent — deliberately NOT the GitHub auth token, to avoid leaking
# credentials to third-party domains (issue #40 constraint).
USER_AGENT = "agentic-developer-core/worker fetch_url tool"

# SSRF error prefixes surfaced to the Worker.
_SSRF_BLOCKED_MSG = (
    "Error: URL '{url}' is blocked by SSRF protection "
    "(private/loopback/link-local/reserved/multicast/unspecified address)."
)
_STRICT_BLOCKED_MSG = "Error: Access to URL '{url}' restricted under strict mode."
_SSRF_REDIRECT_BLOCKED_MSG = (
    "Error: redirect target '{url}' is blocked by SSRF protection."
)
_STRICT_REDIRECT_BLOCKED_MSG = (
    "Error: redirect target '{url}' restricted under strict mode."
)


class _Redirect(Exception):
    """Raised by the no-redirect handler to surface a 3xx Location to the
    fetch loop, which validates the new URL before following it."""

    def __init__(self, new_url: str, code: int) -> None:
        super().__init__(new_url)
        self.new_url = new_url
        self.code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Replace urllib's default redirect-following handler so the fetch loop
    owns redirect traversal and can validate each hop's resolved IP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise _Redirect(newurl, code)


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for private/loopback/link-local/reserved/multicast/unspecified IPs."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_validated_ip(hostname: str) -> str | None:
    """Resolve ``hostname`` once and return the first validated IP, or None.

    This is the single DNS resolution used for BOTH the SSRF gate and the
    connection pin (ADR-0037 layer 3). Validating every resolved address and
    returning the first safe one means there is no second resolution at
    connect time — so a DNS-rebinding TOCTOU (TTL-0 record flip between
    validation and connect, CVE-2026-41488 / CVE-2026-12075) cannot reach a
    private/internal address. Obfuscated IP forms (e.g. ``2130706433``) are
    normalized by ``getaddrinfo`` before the check.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    if not infos:
        return None
    validated: str | None = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            # Unparseable address record — fail closed.
            return None
        if _ip_is_unsafe(ip):
            return None
        if validated is None:
            validated = addr
    return validated


def is_ssrf_safe_url(url: str) -> bool:
    """Return True iff ``url`` has an http/https scheme and *every* resolved
    IP address of its host is non-private / non-loopback / non-link-local /
    non-reserved / non-multicast / non-unspecified.

    Resolving all addresses (not just the first) defends against DNS
    round-robin rebinding where a host resolves to both a public and a
    private address (drawbridge / Stytch). Obfuscated IP forms
    (e.g. ``http://2130706433/``) are normalized by ``getaddrinfo`` before
    the ``ipaddress`` check, so they cannot bypass validation.

    Thin wrapper over :func:`_resolve_validated_ip`; the resolution is shared
    with the connection pin so the gate and the connect use the same address.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    return _resolve_validated_ip(hostname) is not None


def is_domain_allowed(url: str, sources: SourcesConfig) -> bool:
    """Return True iff ``url`` passes the optional strict-mode allowlist.

    Under strict mode, a URL is allowed when it exactly matches an entry in
    ``sources.urls`` (scheme/host/path comparison) or when its hostname is an
    exact or subdomain match of an entry in ``sources.domains``. In
    non-strict mode the allowlist is bypassed (SSRF protection still applies
    in ``is_url_allowed``).
    """
    if not sources.strict:
        return True
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    url_norm = url.strip().lower()
    for allowed_url in sources.urls:
        if url_norm == allowed_url.strip().lower():
            return True
    for domain in sources.domains:
        d = domain.strip().lower()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def is_url_allowed(url: str, sources: SourcesConfig) -> bool:
    """Combine SSRF safety and strict-mode allowlisting.

    SSRF protection is always enforced; the domain allowlist is an additional
    restriction applied only under strict mode.
    """
    return is_ssrf_safe_url(url) and is_domain_allowed(url, sources)


def _fetch_once(url: str, opener: urllib.request.OpenerDirector):
    """Issue a single non-redirecting GET and return ``(status, headers, text)``.

    Reads at most ``MAX_FETCH_BYTES`` so a large body cannot exhaust memory.
    Decodes using the response's declared charset (UTF-8 fallback) with
    ``errors="replace"`` so exotic encodings never crash the Worker.

    The request URL keeps the original hostname (so HTTPS SNI and certificate
    validation use the real host); the IP pinning happens inside the opener's
    custom connection classes (see :func:`_build_pinned_opener`).
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(MAX_FETCH_BYTES)
        status = resp.status
        headers = resp.headers
    charset = headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    return status, headers, text


def _truncate(text: str) -> str:
    """Truncate to ``MAX_FETCH_CHARS`` characters, appending a note when the
    body exceeded the cap (i.e. the byte read budget was saturated)."""
    if len(text) > MAX_FETCH_CHARS:
        return text[:MAX_FETCH_CHARS] + TRUNCATION_NOTE
    return text


# ---------------------------------------------------------------------------
# DNS pinning (ADR-0037 layer 3 / CVE-2026-41488 fix pattern).
#
# The opener's HTTP/HTTPS connection classes TCP-connect to a pre-resolved,
# validated IP instead of letting urllib re-resolve the hostname at connect
# time. For HTTPS, the TLS handshake still uses the original hostname for SNI
# and certificate validation (``server_hostname=self.host``), so pinned https
# fetches remain valid. There is exactly one DNS resolution per hop (in
# ``_resolve_validated_ip``), so a TTL-0 rebinding record cannot flip the
# address between validation and connect.
# ---------------------------------------------------------------------------


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that TCP-connects to a pre-resolved, validated IP."""

    def __init__(self, host, pinned_ip, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that TCP-connects to a pre-resolved, validated IP while
    preserving the original hostname for SNI and certificate validation."""

    def __init__(self, host, pinned_ip, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # ``_context`` is set by http.client.HTTPSConnection.__init__ (a stdlib
        # runtime attribute not declared in the type stubs) and carries the SSL
        # context used for SNI / cert validation.
        self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
            sock, server_hostname=self.host
        )


def _build_pinned_opener(pinned_ip: str) -> urllib.request.OpenerDirector:
    """Build an opener whose HTTP/HTTPS connections pin the validated IP.

    A fresh opener is built per hop because each redirect target may resolve to
    a different (validated) IP. The no-redirect handler is included so the fetch
    loop owns redirect traversal and re-pins every hop.
    """

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(
                lambda host, **kw: _PinnedHTTPConnection(host, pinned_ip, **kw),
                req,
            )

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                lambda host, **kw: _PinnedHTTPSConnection(host, pinned_ip, **kw),
                req,
            )

    return urllib.request.build_opener(
        _NoRedirectHandler(), _PinnedHTTPHandler(), _PinnedHTTPSHandler()
    )


def _pin_url(url: str) -> tuple[str, str] | None:
    """Resolve+validate the URL's host once and return ``(url, validated_ip)``.

    Returns None if the scheme is disallowed, the host is absent, resolution
    fails, or any resolved IP is unsafe (SSRF block). The URL is returned
    unchanged so HTTPS SNI/cert validation use the real hostname; the validated
    IP is pinned at the connection layer by :func:`_build_pinned_opener`.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ALLOWED_SCHEMES:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    pinned_ip = _resolve_validated_ip(hostname)
    if pinned_ip is None:
        return None
    return url, pinned_ip


@tool
def fetch_url(url: str) -> str:
    """Fetch the text content of a URL (e.g. a documentation page found via web_search).

    Use this AFTER `web_search` when you need the full content of a documentation page,
    API reference, or article referenced in the search results. The fetch is SSRF-protected
    (private/internal addresses are blocked), DNS is pinned per hop (no rebinding TOCTOU),
    and responses over 50,000 characters are truncated. Returns the page text, or an error
    message beginning with 'Error:'.
    """
    sources = load_sources_config()

    # Strict-mode allowlist is host-level (no resolution needed).
    if not is_domain_allowed(url, sources):
        return _STRICT_BLOCKED_MSG.format(url=url)

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        # DNS pin (ADR-0037 layer 3): resolve+validate once per hop and connect
        # to the validated IP. No second resolution → no rebinding TOCTOU.
        pinned = _pin_url(current)
        if pinned is None:
            if current == url:
                return _SSRF_BLOCKED_MSG.format(url=url)
            return _SSRF_REDIRECT_BLOCKED_MSG.format(url=current)
        pinned_url, pinned_ip = pinned
        opener = _build_pinned_opener(pinned_ip)

        try:
            status, headers, text = _fetch_once(pinned_url, opener)
        except _Redirect as r:
            new_url = urljoin(current, r.new_url)
            # Re-validate every redirect hop before following it (issue #40
            # edge case: redirect to a private/internal IP).
            if not is_domain_allowed(new_url, sources):
                return _STRICT_REDIRECT_BLOCKED_MSG.format(url=new_url)
            current = new_url
            continue
        except urllib.error.HTTPError as e:
            return f"Error: HTTP {e.code} fetching '{current}'."
        except socket.timeout:
            return f"Error: request to '{current}' timed out."
        except urllib.error.URLError as e:
            return f"Error fetching URL '{current}': {e.reason}"
        except Exception as e:  # noqa: BLE001 — never crash the Worker
            return f"Error fetching URL '{current}': {e}"

        # Non-text (binary) detection: a null byte in the leading content
        # indicates the body is not decodable text the Worker can reason over.
        if "\0" in text[:1024]:
            return f"Error: URL '{current}' returned non-text (binary) content."

        logger.info(
            "fetch_url retrieved '%s' (status=%s, chars=%d)", current, status, len(text)
        )
        return _truncate(text)

    return f"Error: too many redirects (>{MAX_REDIRECTS}) when fetching '{url}'."
