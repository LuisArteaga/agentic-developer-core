import json
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.research_tools import (
    MAX_SNIPPET_CHARS,
    _build_search_tool_parameters,
    _coerce_text,
    _extract_json_block,
    _parse_search_results,
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


if __name__ == "__main__":
    unittest.main()
