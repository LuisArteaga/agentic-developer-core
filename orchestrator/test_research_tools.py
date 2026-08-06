import json
import socket
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.research_tools import (
    MAX_FETCH_CHARS,
    MAX_SNIPPET_CHARS,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    _build_search_tool_parameters,
    _coerce_text,
    _extract_json_block,
    _parse_search_results,
    _pin_url,
    _resolve_validated_ip,
    fetch_url,
    is_domain_allowed,
    is_ssrf_safe_url,
    is_url_allowed,
    web_search,
)
from orchestrator.sources_config import SearchParametersConfig, SourcesConfig


class TestBuildSearchToolParameters(unittest.TestCase):
    def test_minimal_non_strict(self):
        cfg = SourcesConfig()  # non-strict, defaults
        params = _build_search_tool_parameters(cfg)
        self.assertEqual(params, {"engine": "auto"})

    def test_all_optional_fields(self):
        cfg = SourcesConfig(
            search=SearchParametersConfig(
                engine="exa",
                search_context_size="high",
                max_results=5,
                max_total_results=15,
                excluded_domains=["reddit.com"],
            )
        )
        params = _build_search_tool_parameters(cfg)
        self.assertEqual(params["engine"], "exa")
        self.assertEqual(params["search_context_size"], "high")
        self.assertEqual(params["max_results"], 5)
        self.assertEqual(params["max_total_results"], 15)
        self.assertEqual(params["excluded_domains"], ["reddit.com"])
        self.assertNotIn("allowed_domains", params)

    def test_strict_adds_allowed_domains(self):
        cfg = SourcesConfig(strict=True, domains=["arxiv.org", "pypi.org"])
        params = _build_search_tool_parameters(cfg)
        self.assertEqual(params["allowed_domains"], ["arxiv.org", "pypi.org"])

    def test_non_strict_omits_allowed_domains_even_with_domains(self):
        """allowed_domains is only added under strict mode."""
        cfg = SourcesConfig(strict=False, domains=["arxiv.org"])
        params = _build_search_tool_parameters(cfg)
        self.assertNotIn("allowed_domains", params)


class TestExtractJsonBlock(unittest.TestCase):
    def test_fenced_json_block(self):
        text = 'Some prose.\n```json\n[{"title": "T", "url": "u", "snippet": "s"}]\n```\nDone.'
        self.assertEqual(
            _extract_json_block(text),
            '[{"title": "T", "url": "u", "snippet": "s"}]',
        )

    def test_bare_array_fallback(self):
        text = 'Results: [{"title": "T", "url": "u"}] end'
        self.assertEqual(_extract_json_block(text), '[{"title": "T", "url": "u"}]')

    def test_returns_text_when_no_block(self):
        self.assertEqual(_extract_json_block("[1, 2]"), "[1, 2]")


class TestCoerceText(unittest.TestCase):
    def test_str_passthrough(self):
        self.assertEqual(_coerce_text("hello"), "hello")

    def test_list_of_text_blocks(self):
        self.assertEqual(
            _coerce_text(
                [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
            ),
            "ab",
        )

    def test_list_with_str_entries(self):
        self.assertEqual(_coerce_text(["x", "y"]), "xy")

    def test_none(self):
        self.assertEqual(_coerce_text(None), "")

    def test_other_type(self):
        self.assertEqual(_coerce_text(123), "123")


class TestParseSearchResults(unittest.TestCase):
    def test_parses_valid_list(self):
        text = '```json\n[{"title": "T1", "url": "u1", "snippet": "s1"}]\n```'
        results = _parse_search_results(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], {"title": "T1", "url": "u1", "snippet": "s1"})

    def test_truncates_long_snippets(self):
        long_snippet = "x" * (MAX_SNIPPET_CHARS + 50)
        text = json.dumps([{"title": "T", "url": "u", "snippet": long_snippet}])
        results = _parse_search_results(text)
        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(results[0]["snippet"]), MAX_SNIPPET_CHARS)
        self.assertTrue(results[0]["snippet"].endswith("..."))

    def test_exactly_at_limit_not_truncated(self):
        snippet = "x" * MAX_SNIPPET_CHARS
        text = json.dumps([{"title": "T", "url": "u", "snippet": snippet}])
        results = _parse_search_results(text)
        self.assertEqual(len(results[0]["snippet"]), MAX_SNIPPET_CHARS)

    def test_filters_items_without_url(self):
        text = json.dumps(
            [
                {"title": "T", "url": "u", "snippet": "s"},
                {"title": "NoUrl", "snippet": "s"},
            ]
        )
        results = _parse_search_results(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "u")

    def test_defaults_title_when_missing(self):
        text = json.dumps([{"url": "u", "snippet": "s"}])
        results = _parse_search_results(text)
        self.assertEqual(results[0]["title"], "No Title")

    def test_invalid_json_returns_empty(self):
        self.assertEqual(_parse_search_results("not json at all"), [])

    def test_non_list_json_returns_empty(self):
        self.assertEqual(_parse_search_results('{"a": 1}'), [])

    def test_empty_list(self):
        self.assertEqual(_parse_search_results("[]"), [])


class TestWebSearchTool(unittest.TestCase):
    """End-to-end behavior of the web_search @tool with the LLM mocked."""

    def _make_response(self, content):
        resp = MagicMock()
        resp.content = content
        return resp

    def _patch_stack(self, response, sources=None):
        """Patch the four collaborators web_search depends on."""
        sources = sources if sources is not None else SourcesConfig()
        bound = MagicMock()
        bound.invoke.return_value = response
        llm = MagicMock()
        llm.bind.return_value = bound
        cfg = {
            "model": "deepseek/deepseek-v4-flash",
            "routing": ["DeepInfra"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": None,
        }
        return (
            patch(
                "orchestrator.research_tools.load_sources_config", return_value=sources
            ),
            patch("orchestrator.research_tools.resolve_model_config", return_value=cfg),
            patch(
                "orchestrator.research_tools.get_chat_model_from_config",
                return_value=llm,
            ),
            llm,
            bound,
        )

    def test_tool_name_and_doc(self):
        self.assertEqual(web_search.name, "web_search")
        self.assertIn("Search the web", web_search.description)

    def test_returns_json_results(self):
        response = self._make_response(
            '```json\n[{"title": "T", "url": "https://e.com", "snippet": "s"}]\n```'
        )
        p_load, p_resolve, p_chat, llm, bound = self._patch_stack(response)
        with p_load, p_resolve, p_chat:
            out = web_search.invoke({"query": "langgraph create_react_agent"})
        parsed = json.loads(out)
        self.assertEqual(parsed[0]["url"], "https://e.com")
        # tool_choice forced to guarantee execution
        self.assertEqual(
            llm.bind.call_args.kwargs["tool_choice"],
            {"type": "openrouter:web_search"},
        )
        self.assertEqual(
            llm.bind.call_args.kwargs["tools"][0]["type"],
            "openrouter:web_search",
        )
        bound.invoke.assert_called_once()

    def test_no_results_returns_clear_message(self):
        response = self._make_response("```json\n[]\n```")
        p_load, p_resolve, p_chat, llm, bound = self._patch_stack(response)
        with p_load, p_resolve, p_chat:
            out = web_search.invoke({"query": "obscure query xyz"})
        self.assertEqual(out, "No search results found for query: obscure query xyz")

    def test_unparseable_output_returns_no_results(self):
        response = self._make_response("I could not search.")
        p_load, p_resolve, p_chat, llm, bound = self._patch_stack(response)
        with p_load, p_resolve, p_chat:
            out = web_search.invoke({"query": "q"})
        self.assertEqual(out, "No search results found for query: q")

    def test_invocation_error_returns_error_message(self):
        bound = MagicMock()
        bound.invoke.side_effect = RuntimeError("boom")
        llm = MagicMock()
        llm.bind.return_value = bound
        cfg = {
            "model": "m",
            "routing": None,
            "temperature": 0.0,
            "options": None,
            "fallback_model": None,
        }
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=SourcesConfig(),
            ),
            patch("orchestrator.research_tools.resolve_model_config", return_value=cfg),
            patch(
                "orchestrator.research_tools.get_chat_model_from_config",
                return_value=llm,
            ),
        ):
            out = web_search.invoke({"query": "q"})
        self.assertIn("Error executing web_search for query 'q'", out)
        self.assertIn("boom", out)

    def test_strict_mode_passes_allowed_domains_in_user_message(self):
        response = self._make_response("```json\n[]\n```")
        sources = SourcesConfig(strict=True, domains=["arxiv.org"])
        p_load, p_resolve, p_chat, llm, bound = self._patch_stack(response, sources)
        with p_load, p_resolve, p_chat:
            web_search.invoke({"query": "q"})
        # The HumanMessage content should mention the strict allowed domains.
        human_msg = bound.invoke.call_args.args[0][1]
        self.assertIn("Allowed Domains", human_msg.content)
        self.assertIn("arxiv.org", human_msg.content)

    def test_strict_allowed_domains_added_to_tool_parameters(self):
        response = self._make_response("```json\n[]\n```")
        sources = SourcesConfig(strict=True, domains=["arxiv.org"])
        p_load, p_resolve, p_chat, llm, bound = self._patch_stack(response, sources)
        with p_load, p_resolve, p_chat:
            web_search.invoke({"query": "q"})
        tool_def = llm.bind.call_args.kwargs["tools"][0]
        self.assertEqual(tool_def["parameters"]["allowed_domains"], ["arxiv.org"])


def _gaia(addr):
    """Build a getaddrinfo-style record list for a single resolved IP."""
    # getaddrinfo returns 5-tuples; index 4 is the sockaddr, whose [0] is the IP.
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))]


class TestIsSsrfSafeUrl(unittest.TestCase):
    def _patch_gai(self, addrs):
        return patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=lambda host, port: (
                _gaia(addrs)
                if isinstance(addrs, str)
                else sum((_gaia(a) for a in addrs), [])
            ),
        )

    def test_public_ip_is_safe(self):
        with self._patch_gai("93.184.216.34"):
            self.assertTrue(is_ssrf_safe_url("https://example.com/"))

    def test_loopback_blocked(self):
        with self._patch_gai("127.0.0.1"):
            self.assertFalse(is_ssrf_safe_url("http://localhost/"))

    def test_ipv6_loopback_blocked(self):
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))
            ],
        ):
            self.assertFalse(is_ssrf_safe_url("http://[::1]/"))

    def test_link_local_blocked(self):
        # 169.254.169.254 — AWS / GCP cloud metadata endpoint.
        with self._patch_gai("169.254.169.254"):
            self.assertFalse(
                is_ssrf_safe_url("http://169.254.169.254/latest/meta-data/")
            )

    def test_private_10_blocked(self):
        with self._patch_gai("10.0.0.1"):
            self.assertFalse(is_ssrf_safe_url("http://internal.corp/"))

    def test_reserved_blocked(self):
        with self._patch_gai("240.0.0.1"):
            self.assertFalse(is_ssrf_safe_url("http://r.example/"))

    def test_multicast_blocked(self):
        with self._patch_gai("224.0.0.1"):
            self.assertFalse(is_ssrf_safe_url("http://mcast.example/"))

    def test_unspecified_blocked(self):
        with self._patch_gai("0.0.0.0"):
            self.assertFalse(is_ssrf_safe_url("http://0.0.0.0/"))

    def test_any_resolved_ip_unsafe_blocks(self):
        # DNS round-robin rebinding: one public, one private -> reject.
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=lambda host, port: _gaia("93.184.216.34") + _gaia("10.0.0.1"),
        ):
            self.assertFalse(is_ssrf_safe_url("http://evil.example/"))

    def test_obfuscated_decimal_ip_blocked(self):
        # 2130706433 decimal form resolves to 127.0.0.1 via getaddrinfo.
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=lambda host, port: (
                _gaia("127.0.0.1") if host == "2130706433" else _gaia("1.1.1.1")
            ),
        ):
            self.assertFalse(is_ssrf_safe_url("http://2130706433/"))

    def test_bad_scheme_blocked(self):
        with self._patch_gai("1.1.1.1"):
            for bad in ("file:///etc/passwd", "ftp://example.com/", "gopher://x/"):
                self.assertFalse(is_ssrf_safe_url(bad))

    def test_no_hostname_blocked(self):
        self.assertFalse(is_ssrf_safe_url("https:///path"))

    def test_dns_failure_blocked(self):
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ):
            self.assertFalse(is_ssrf_safe_url("https://nonexistent.invalid/"))


class TestIsDomainAllowed(unittest.TestCase):
    def test_non_strict_allows_all(self):
        sources = SourcesConfig(strict=False)
        self.assertTrue(is_domain_allowed("https://anything.example/x", sources))

    def test_strict_exact_url_match(self):
        sources = SourcesConfig(strict=True, urls=["https://docs.python.org/3/"])
        self.assertTrue(is_domain_allowed("https://docs.python.org/3/", sources))
        self.assertFalse(is_domain_allowed("https://docs.python.org/2/", sources))

    def test_strict_exact_domain_match(self):
        sources = SourcesConfig(strict=True, domains=["arxiv.org"])
        self.assertTrue(is_domain_allowed("https://arxiv.org/abs/1", sources))

    def test_strict_subdomain_match(self):
        sources = SourcesConfig(strict=True, domains=["python.org"])
        self.assertTrue(is_domain_allowed("https://docs.python.org/3/", sources))

    def test_strict_non_match_blocked(self):
        sources = SourcesConfig(strict=True, domains=["python.org"])
        self.assertFalse(is_domain_allowed("https://evil.com/", sources))

    def test_strict_substring_not_a_subdomain(self):
        # "notpython.org" must not match "python.org".
        sources = SourcesConfig(strict=True, domains=["python.org"])
        self.assertFalse(is_domain_allowed("https://notpython.org/", sources))


class TestIsUrlAllowed(unittest.TestCase):
    def test_combines_ssrf_and_strict(self):
        sources = SourcesConfig(strict=True, domains=["example.com"])
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            return_value=_gaia("93.184.216.34"),
        ):
            self.assertTrue(is_url_allowed("https://example.com/", sources))
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            return_value=_gaia("10.0.0.1"),
        ):
            self.assertFalse(is_url_allowed("https://example.com/", sources))


class TestFetchUrlTool(unittest.TestCase):
    def _sources(self, strict=False, **kw):
        # Strict mode requires at least one domain/url to pass Pydantic validation.
        if strict and not kw.get("domains") and not kw.get("urls"):
            kw["domains"] = ["allowed.example"]
        return SourcesConfig(strict=strict, **kw)

    def _patch_ssrf(self, safe=True):
        return patch("orchestrator.research_tools.is_ssrf_safe_url", return_value=safe)

    def _patch_domain(self, allowed=True):
        return patch(
            "orchestrator.research_tools.is_domain_allowed", return_value=allowed
        )

    def test_tool_name_and_doc(self):
        self.assertEqual(fetch_url.name, "fetch_url")
        self.assertIn("Fetch the text content", fetch_url.description)

    def test_ssrf_blocked_start_url(self):
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip", return_value=None
            ),
        ):
            out = fetch_url.invoke({"url": "http://169.254.169.254/"})
        self.assertIn("blocked by SSRF protection", out)
        self.assertIn("169.254.169.254", out)

    def test_strict_mode_blocks_unlisted_url(self):
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(strict=True),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=False),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("restricted under strict mode", out)

    def test_successful_fetch_returns_text(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"<html>hello</html>"
        fake_resp.status = 200
        fake_resp.headers.get_content_charset.return_value = "utf-8"
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = fake_resp
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertEqual(out, "<html>hello</html>")

    def test_truncates_long_response(self):
        body = "x" * (MAX_FETCH_CHARS + 500)
        fake_resp = MagicMock()
        fake_resp.read.return_value = body.encode()
        fake_resp.status = 200
        fake_resp.headers.get_content_charset.return_value = "utf-8"
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = fake_resp
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertEqual(
            len(out), MAX_FETCH_CHARS + len("[truncated due to length]") + 1
        )  # +1 for leading \n
        self.assertTrue(out.endswith("[truncated due to length]"))

    def test_binary_content_returns_error(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"\x00\x01\x02binary\xff"
        fake_resp.status = 200
        fake_resp.headers.get_content_charset.return_value = "utf-8"
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = fake_resp
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/img"})
        self.assertIn("non-text", out)

    def test_redirect_to_safe_url_followed(self):
        # First call raises _Redirect to a safe URL; second returns content.
        from orchestrator.research_tools import _Redirect

        good_resp = MagicMock()
        good_resp.read.return_value = b"<html>final</html>"
        good_resp.status = 200
        good_resp.headers.get_content_charset.return_value = "utf-8"
        good_resp.__enter__ = MagicMock(return_value=good_resp)
        good_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.side_effect = [
            _Redirect("https://docs.example.org/page", 302),
            good_resp,
        ]
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertEqual(out, "<html>final</html>")
        # is_ssrf_safe_url called once for the start URL + once for the redirect target.
        self.assertGreaterEqual(opener.open.call_count, 2)

    def test_redirect_to_private_ip_blocked(self):
        from orchestrator.research_tools import _Redirect

        opener = MagicMock()
        opener.open.side_effect = [_Redirect("http://169.254.169.254/", 302)]
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                side_effect=["93.184.216.34", None],
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("redirect target", out)
        self.assertIn("blocked by SSRF protection", out)

    def test_http_error_returns_error(self):
        import email.message
        import urllib.error

        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.com/", 404, "Not Found", email.message.Message(), None
        )
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/missing"})
        self.assertIn("HTTP 404", out)

    def test_timeout_returns_error(self):
        opener = MagicMock()
        opener.open.side_effect = socket.timeout()
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("timed out", out)

    def test_too_many_redirects(self):
        from orchestrator.research_tools import MAX_REDIRECTS, _Redirect

        opener = MagicMock()
        # Every hop redirects; loops MAX_REDIRECTS+1 times then gives up.
        opener.open.side_effect = [_Redirect("https://example.com/p", 302)] * (
            MAX_REDIRECTS + 1
        )
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("too many redirects", out)

    def test_urlerror_returns_error(self):
        """A URLError from the opener is surfaced as an Error (ADR-0037 #4)."""
        import urllib.error

        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("dns failed")
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("Error fetching URL", out)
        self.assertIn("dns failed", out)

    def test_generic_exception_returns_error(self):
        """A non-HTTP/non-URL exception is caught and surfaced (never crashes)."""
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("boom")
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch("orchestrator.research_tools.is_domain_allowed", return_value=True),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://example.com/"})
        self.assertIn("Error fetching URL", out)
        self.assertIn("boom", out)

    def test_strict_mode_redirect_to_unlisted_url_blocked(self):
        """Under strict mode, a redirect to a non-allowlisted URL is blocked."""
        from orchestrator.research_tools import _Redirect

        # Strict mode with one allowed domain; the redirect target is unlisted.
        sources = self._sources(strict=True, domains=["allowed.example"])

        def domain_allowed(url, src):
            # Real is_domain_allowed under strict: allow allowed.example, block others.
            return "allowed.example" in url

        opener = MagicMock()
        opener.open.side_effect = [_Redirect("https://evil.example/", 302)]
        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=sources,
            ),
            patch(
                "orchestrator.research_tools._resolve_validated_ip",
                return_value="93.184.216.34",
            ),
            patch(
                "orchestrator.research_tools.is_domain_allowed",
                side_effect=domain_allowed,
            ),
            patch(
                "orchestrator.research_tools.urllib.request.build_opener",
                return_value=opener,
            ),
        ):
            out = fetch_url.invoke({"url": "https://allowed.example/"})
        self.assertIn("restricted under strict mode", out)
        self.assertIn("evil.example", out)

    def test_dns_pinning_no_rebinding_to_private_ip(self):
        """CVE-2026-41488 regression: a rebinding-style double-resolution
        cannot reach a private IP. The first resolution returns a public IP; a
        would-be second resolution returns a private IP. With DNS pinning the
        connection uses the validated public IP and there is NO second
        resolution, so the private address is never reached (ADR-0037 #4)."""
        captured: dict = {}

        def fake_opener(pinned_ip):
            captured["ip"] = pinned_ip
            opener = MagicMock()
            resp = MagicMock()
            resp.read.return_value = b"<html>ok</html>"
            resp.status = 200
            resp.headers.get_content_charset.return_value = "utf-8"
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            opener.open.return_value = resp
            return opener

        gai_calls: list = []

        def fake_gai(host, port):
            gai_calls.append(host)
            # Only ever return a public IP. If a second resolution occurred it
            # would (in this test) also be public, but the assertion is on the
            # call count: pinning must do exactly one resolution.
            return _gaia("93.184.216.34")

        with (
            patch(
                "orchestrator.research_tools.load_sources_config",
                return_value=self._sources(),
            ),
            patch(
                "orchestrator.research_tools.socket.getaddrinfo",
                side_effect=fake_gai,
            ),
            patch(
                "orchestrator.research_tools._build_pinned_opener",
                side_effect=fake_opener,
            ),
        ):
            out = fetch_url.invoke({"url": "http://example.com/"})
        self.assertEqual(out, "<html>ok</html>")
        # Pinned to the validated public IP.
        self.assertEqual(captured["ip"], "93.184.216.34")
        # Exactly one DNS resolution — no second (rebinding) lookup.
        self.assertEqual(len(gai_calls), 1)


class TestResolveValidatedIp(unittest.TestCase):
    def _patch_gai(self, addrs):
        return patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=lambda host, port: (
                _gaia(addrs)
                if isinstance(addrs, str)
                else sum((_gaia(a) for a in addrs), [])
            ),
        )

    def test_returns_first_public_ip(self):
        with self._patch_gai("93.184.216.34"):
            self.assertEqual(_resolve_validated_ip("example.com"), "93.184.216.34")

    def test_returns_none_for_private(self):
        with self._patch_gai("10.0.0.1"):
            self.assertIsNone(_resolve_validated_ip("internal.corp"))

    def test_returns_none_on_round_robin_with_private(self):
        # DNS round-robin rebinding: one public + one private -> reject (None).
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=lambda host, port: _gaia("93.184.216.34") + _gaia("10.0.0.1"),
        ):
            self.assertIsNone(_resolve_validated_ip("evil.example"))

    def test_returns_none_on_dns_failure(self):
        with patch(
            "orchestrator.research_tools.socket.getaddrinfo",
            side_effect=socket.gaierror,
        ):
            self.assertIsNone(_resolve_validated_ip("nonexistent.invalid"))


class TestPinnedConnections(unittest.TestCase):
    def test_http_connect_uses_pinned_ip(self):
        with patch("orchestrator.research_tools.socket.create_connection") as mock_cc:
            conn = _PinnedHTTPConnection("example.com", "93.184.216.34")
            conn.port = 80
            conn.timeout = 5
            conn.connect()
        mock_cc.assert_called_once_with(("93.184.216.34", 80), 5)

    def test_https_connect_pins_ip_and_preserves_sni(self):
        fake_sock = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.wrap_socket.return_value = MagicMock()
        with patch(
            "orchestrator.research_tools.socket.create_connection",
            return_value=fake_sock,
        ) as mock_cc:
            # Pass the fake SSL context via the constructor (the stdlib sets
            # ``_context`` from it) so connect() uses it without a real handshake.
            conn = _PinnedHTTPSConnection(
                "example.com", "93.184.216.34", context=fake_ctx
            )
            conn.port = 443
            conn.timeout = 5
            conn.connect()
        # TCP connects to the pinned validated IP...
        mock_cc.assert_called_once_with(("93.184.216.34", 443), 5)
        # ...while TLS SNI uses the original hostname (cert validation intact).
        fake_ctx.wrap_socket.assert_called_once_with(
            fake_sock, server_hostname="example.com"
        )

    def test_build_pinned_opener_http_handler_uses_pinned_ip(self):
        """The opener's HTTP handler builds a _PinnedHTTPConnection carrying the
        pinned IP when it opens a request (ADR-0037 #4)."""
        import urllib.request

        from orchestrator.research_tools import _build_pinned_opener

        opener = _build_pinned_opener("1.2.3.4")
        # OpenerDirector.handlers exists at runtime but is not in the type stubs.
        handlers = getattr(opener, "handlers", [])
        http_handler = next(
            h for h in handlers if isinstance(h, urllib.request.HTTPHandler)
        )
        captured = {}

        def fake_do_open(self, http_class, req):
            # Invoke the connection factory (the lambda) the real do_open would.
            captured["conn"] = http_class("example.com")
            return MagicMock()

        with patch.object(urllib.request.HTTPHandler, "do_open", fake_do_open):
            http_handler.http_open(MagicMock())
        self.assertIsInstance(captured["conn"], _PinnedHTTPConnection)
        self.assertEqual(captured["conn"]._pinned_ip, "1.2.3.4")

    def test_build_pinned_opener_https_handler_uses_pinned_ip(self):
        """The opener's HTTPS handler builds a _PinnedHTTPSConnection with the
        pinned IP (ADR-0037 #4)."""
        import urllib.request

        from orchestrator.research_tools import _build_pinned_opener

        opener = _build_pinned_opener("5.6.7.8")
        # OpenerDirector.handlers exists at runtime but is not in the type stubs.
        handlers = getattr(opener, "handlers", [])
        https_handler = next(
            h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)
        )
        captured = {}

        def fake_do_open(self, http_class, req):
            captured["conn"] = http_class("example.com")
            return MagicMock()

        with patch.object(urllib.request.HTTPSHandler, "do_open", fake_do_open):
            https_handler.https_open(MagicMock())
        self.assertIsInstance(captured["conn"], _PinnedHTTPSConnection)
        self.assertEqual(captured["conn"]._pinned_ip, "5.6.7.8")


class TestPinUrl(unittest.TestCase):
    """DNS pinning: _pin_url resolves+validates once and returns (url, ip)."""

    def test_returns_none_for_bad_scheme(self):
        self.assertIsNone(_pin_url("file:///etc/passwd"))

    def test_returns_none_for_no_host(self):
        self.assertIsNone(_pin_url("https:///path"))

    def test_returns_none_on_ssrf_block(self):
        with patch(
            "orchestrator.research_tools._resolve_validated_ip", return_value=None
        ):
            self.assertIsNone(_pin_url("http://169.254.169.254/"))

    def test_returns_url_and_ip_for_public(self):
        with patch(
            "orchestrator.research_tools._resolve_validated_ip",
            return_value="93.184.216.34",
        ):
            self.assertEqual(
                _pin_url("https://example.com/path"),
                ("https://example.com/path", "93.184.216.34"),
            )

    def test_returns_none_on_urlsplit_error(self):
        """A malformed URL that urlsplit rejects yields None (defensive)."""
        with patch(
            "orchestrator.research_tools.urlsplit", side_effect=ValueError("bad")
        ):
            self.assertIsNone(_pin_url("https://example.com/"))


if __name__ == "__main__":
    unittest.main()
