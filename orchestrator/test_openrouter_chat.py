"""Tests for ``orchestrator.openrouter_chat``.

Covers the ``OpenRouterAnnotationChatOpenAI`` subclass's additive
``_create_chat_result`` override (ADR-0039) and the ``_extract_url_citations``
helper the ``web_search`` Worker Tool consumes.
"""

import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from pydantic import SecretStr

from orchestrator.openrouter_chat import (
    MAX_SNIPPET_CHARS,
    OpenRouterAnnotationChatOpenAI,
    _extract_url_citations,
)


def _url_citation(url, title="T", content="s"):
    """Build a url_citation annotation dict as it appears post-``model_dump``
    (without the SDK-required ``start_index``/``end_index`` fields, which the
    override and ``_extract_url_citations`` never read)."""
    return {
        "type": "url_citation",
        "url_citation": {"title": title, "url": url, "content": content},
    }


def _sdk_url_citation(url, title="T", content="s"):
    """Like ``_url_citation`` but includes the ``start_index``/``end_index``
    fields the openai SDK requires when constructing a ``ChatCompletion``."""
    uc = _url_citation(url, title, content)["url_citation"]
    uc["start_index"] = 0
    uc["end_index"] = 1
    return {"type": "url_citation", "url_citation": uc}


def _make_completion(annotations):
    """Build a real openai ``ChatCompletion`` (the production BaseModel path)
    with the given annotations on the assistant message. ``annotations`` must
    use ``_sdk_url_citation`` (with start/end indices) to pass SDK validation."""
    msg = ChatCompletionMessage(role="assistant", content="hi", annotations=annotations)
    return ChatCompletion(
        id="x",
        object="chat.completion",
        choices=[Choice(index=0, message=msg, finish_reason="stop")],
        created=1,
        model="m",
    )


class TestOpenRouterAnnotationChat(unittest.TestCase):
    """The additive ``_create_chat_result`` override."""

    def setUp(self):
        # Construction is offline (no network/keys needed to exercise the
        # override). api_key is required by the ChatOpenAI validator.
        self.llm = OpenRouterAnnotationChatOpenAI(
            model="m", api_key=SecretStr("k"), use_responses_api=False
        )

    def test_captures_annotations_into_additional_kwargs(self):
        """Annotations present on the response are injected verbatim into
        ``generations[0].message.additional_kwargs["annotations"]``."""
        anns = [
            _sdk_url_citation("https://a.com", "A", "snip-a"),
            _sdk_url_citation("https://b.com", "B", "snip-b"),
        ]
        result = self.llm._create_chat_result(_make_completion(anns))
        captured = result.generations[0].message.additional_kwargs.get("annotations")
        self.assertEqual(captured, anns)

    def test_preserves_openrouter_content_snippet_field(self):
        """The OpenRouter-added ``content`` (snippet) survives ``model_dump``
        via the SDK's ``extra="allow`` config (ADR-0039)."""
        ann = _sdk_url_citation("https://a.com", "A", "the snippet text")
        result = self.llm._create_chat_result(_make_completion([ann]))
        captured = result.generations[0].message.additional_kwargs["annotations"]
        self.assertEqual(captured[0]["url_citation"]["content"], "the snippet text")

    def test_stores_all_annotation_types_raw(self):
        """All annotation types are stored raw; filtering to ``url_citation`` is
        the consumer's job (ADR-0039 §1). Verified via the dict-passthrough
        path, where arbitrary annotation types (e.g. ``file``) can appear."""
        anns = [
            _url_citation("https://a.com"),
            {"type": "file", "file": {"url": "x"}},
        ]
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "annotations": anns,
                    },
                    "finish_reason": "stop",
                }
            ],
            "model": "m",
        }
        result = self.llm._create_chat_result(response)
        self.assertEqual(
            result.generations[0].message.additional_kwargs["annotations"], anns
        )

    def test_noop_when_annotations_absent(self):
        """When the response carries no annotations, the override is a no-op:
        no ``annotations`` key is added and the super result is returned."""
        msg = ChatCompletionMessage(role="assistant", content="hi")
        cc = ChatCompletion(
            id="x",
            object="chat.completion",
            choices=[Choice(index=0, message=msg, finish_reason="stop")],
            created=1,
            model="m",
        )
        result = self.llm._create_chat_result(cc)
        self.assertNotIn("annotations", result.generations[0].message.additional_kwargs)

    def test_noop_when_annotations_is_none(self):
        """``message["annotations"]`` serializes to ``None`` when absent; the
        truthiness guard skips injection."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "annotations": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            "model": "m",
        }
        result = self.llm._create_chat_result(response)
        self.assertNotIn("annotations", result.generations[0].message.additional_kwargs)

    def test_never_raises_on_capture_failure(self):
        """If annotation capture fails for any reason after ``super()`` has
        produced a result, the override must return the super result unmodified
        rather than raising (ADR-0039: degradation is to today's behavior,
        never to a crash). Here ``super()`` succeeds (patched) but the
        re-derivation (``model_dump``) raises."""
        super_result = ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="hi"))]
        )

        class _BadResponse:
            def model_dump(self, **kwargs):
                raise RuntimeError("dump boom")

        with patch.object(ChatOpenAI, "_create_chat_result", return_value=super_result):
            out = self.llm._create_chat_result(_BadResponse())
        self.assertIs(out, super_result)
        # The super result is returned untouched (no annotations injected).
        self.assertNotIn("annotations", out.generations[0].message.additional_kwargs)

    def test_guarded_when_more_choices_than_generations(self):
        """If ``choices`` outnumbers ``generations`` (defensive), the override
        must not raise IndexError — it bounds the injection to available
        generations."""
        anns = [_url_citation("https://a.com")]
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "annotations": anns,
                    },
                    "finish_reason": "stop",
                },
                {  # extra choice with no matching generation
                    "message": {
                        "role": "assistant",
                        "content": "hi2",
                        "annotations": anns,
                    },
                    "finish_reason": "stop",
                },
            ],
            "model": "m",
        }
        # Should not raise.
        result = self.llm._create_chat_result(response)
        self.assertEqual(
            result.generations[0].message.additional_kwargs["annotations"], anns
        )


class TestExtractUrlCitations(unittest.TestCase):
    """The ``_extract_url_citations`` helper: filters to ``url_citation`` and
    extracts ``{title, url, snippet}``."""

    def test_filters_to_url_citation(self):
        anns = [
            _url_citation("https://a.com", "A", "sa"),
            {"type": "file", "file": {"url": "x"}},
            {"type": "url_citation", "url_citation": {"url": "https://b.com"}},
        ]
        results = _extract_url_citations(anns)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {r["url"] for r in results}, {"https://a.com", "https://b.com"}
        )

    def test_extracts_title_url_snippet(self):
        anns = [_url_citation("https://a.com", "Title", "snippet text")]
        results = _extract_url_citations(anns)
        self.assertEqual(
            results[0],
            {"title": "Title", "url": "https://a.com", "snippet": "snippet text"},
        )

    def test_title_fallback_to_no_title(self):
        ann = {"type": "url_citation", "url_citation": {"url": "https://a.com"}}
        results = _extract_url_citations([ann])
        self.assertEqual(results[0]["title"], "No Title")

    def test_snippet_fallback_to_empty_when_content_absent(self):
        ann = {"type": "url_citation", "url_citation": {"url": "https://a.com"}}
        results = _extract_url_citations([ann])
        self.assertEqual(results[0]["snippet"], "")

    def test_truncates_long_snippets(self):
        long_snippet = "x" * (MAX_SNIPPET_CHARS + 50)
        ann = _url_citation("https://a.com", "T", long_snippet)
        results = _extract_url_citations([ann])
        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(results[0]["snippet"]), MAX_SNIPPET_CHARS)
        self.assertTrue(results[0]["snippet"].endswith("..."))

    def test_snippet_at_limit_not_truncated(self):
        snippet = "x" * MAX_SNIPPET_CHARS
        ann = _url_citation("https://a.com", "T", snippet)
        results = _extract_url_citations([ann])
        self.assertEqual(len(results[0]["snippet"]), MAX_SNIPPET_CHARS)

    def test_empty_annotations(self):
        self.assertEqual(_extract_url_citations([]), [])

    def test_none_annotations(self):
        self.assertEqual(_extract_url_citations(None), [])

    def test_ignores_non_dict_annotation(self):
        anns = ["not a dict", 42, None, _url_citation("https://a.com")]
        results = _extract_url_citations(anns)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://a.com")

    def test_url_citation_not_a_dict_is_skipped(self):
        ann = {"type": "url_citation", "url_citation": "not a dict"}
        self.assertEqual(_extract_url_citations([ann]), [])


if __name__ == "__main__":
    unittest.main()
