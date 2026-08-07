"""OpenRouter annotation-capturing ``ChatOpenAI`` subclass.

OpenRouter delivers web-search results **exclusively** via ``url_citation``
annotations on the assistant message (``message.annotations``).
``langchain-openai``'s ``_convert_dict_to_message`` discards these annotations
in Chat Completions mode (``use_responses_api=False``), which ADR-0026
deliberately uses. This module provides a ``ChatOpenAI`` subclass that
additively overrides ``_create_chat_result`` to capture the raw annotations
into ``AIMessage.additional_kwargs["annotations"]`` before langchain drops
them, plus an ``_extract_url_citations`` helper that filters the captured
annotations into the ``[{title, url, snippet}]`` shape the ``web_search``
Worker Tool consumes.

See ADR-0039 for the full rationale and the ``ChatDeepSeek`` precedent that
establishes ``_create_chat_result`` as LangChain's sanctioned extension point
for provider-specific response fields.

Design contract (ADR-0039):
- **Additive**: calls ``super()._create_chat_result`` first (unchanged
  behavior), then injects annotations into the already-built generations.
- **Never raises**: any capture failure is logged and the super result is
  returned unmodified — degradation is to today's behavior (annotations
  lost), never to a crash.
- **Stores all annotation types raw**: filtering to ``url_citation`` is the
  consumer's job (``_extract_url_citations``), not the subclass's.
- **No-op for non-annotation responses**: ``message.get("annotations")``
  returns ``None`` for normal completions, so non-search nodes are
  unaffected by the global construction path (ADR-0039 §1).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

logger = logging.getLogger("orchestrator.openrouter_chat")

# Snippets are truncated to keep the ``web_search`` tool result concise and
# bound the tokens injected back into the Worker's ReAct context. Mirrors
# ``orchestrator/research_tools.MAX_SNIPPET_CHARS``; duplicated here to keep
# this module dependency-free of the research-tools package (and vice-versa:
# research_tools imports the helper from here).
MAX_SNIPPET_CHARS = 300


class OpenRouterAnnotationChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` subclass that captures OpenRouter ``message.annotations``.

    The override is additive and never raises: it first delegates to
    ``super()._create_chat_result`` (unchanged behavior) and then, if the raw
    response carried annotations, injects them into
    ``generations[i].message.additional_kwargs["annotations"]``. Annotations
    are stored raw (all types, including non-``url_citation``); filtering is
    the consumer's responsibility.
    """

    def _create_chat_result(
        self,
        response: "dict | Any",
        generation_info: "dict | None" = None,
    ):  # type: ignore[override]
        # Forward to the parent first so the standard ChatResult (AIMessages,
        # generation_info, llm_output) is built with unchanged behavior. The
        # signature accepts the same two positional args the parent does so
        # that an upstream rename/reshuffle of the private method surfaces as
        # an AttributeError caught below (degrading to today's behavior)
        # rather than a silent mis-wiring.
        result = super()._create_chat_result(response, generation_info)
        try:
            # Re-derive response_dict exactly as the parent does (dict
            # passthrough, else model_dump excluding only the typed `parsed`
            # field) so we can read the raw `message["annotations"]` that
            # `_convert_dict_to_message` skipped. model_dump preserves the
            # OpenRouter-added `content` (snippet) field via the SDK's
            # `extra="allow"` config (empirically verified against
            # openai==2.44.0).
            response_dict = (
                response
                if isinstance(response, dict)
                else response.model_dump(
                    exclude={"choices": {"__all__": {"message": {"parsed"}}}}
                )
            )
            choices = response_dict.get("choices") or []
            generations = result.generations
            for i, res in enumerate(choices):
                if i >= len(generations):
                    break
                annotations = (res.get("message") or {}).get("annotations")
                if annotations:
                    generations[i].message.additional_kwargs["annotations"] = (
                        annotations
                    )
        except Exception as e:  # noqa: BLE001 — never raise (ADR-0039)
            logger.warning(
                "OpenRouter annotation capture failed (degrading to no "
                "annotation capture): %s",
                e,
            )
        return result


def _extract_url_citations(annotations: "list[dict] | None") -> list[dict[str, str]]:
    """Filter raw annotations to ``url_citation`` entries and extract
    ``{title, url, snippet}``.

    - ``url``   ← ``url_citation["url"]``
    - ``title`` ← ``url_citation.get("title", "No Title")`` (title can be
      missing per vercel/ai #10199; the SDK declares it required, so a missing
      title fails SDK validation before this helper runs — the fallback is
      defensive for the dict-passthrough path).
    - ``snippet`` ← ``url_citation.get("content", "")`` (OpenRouter adds
      ``content`` "if available"; preserved via the SDK's ``extra="allow"``),
      truncated to ``MAX_SNIPPET_CHARS``.

    Non-``url_citation`` annotation types (e.g. ``file``) are ignored. The
    ``url_citation`` payload must be a dict; otherwise the annotation is
    skipped defensively.
    """
    results: list[dict[str, str]] = []
    for ann in annotations or []:
        if not isinstance(ann, dict) or ann.get("type") != "url_citation":
            continue
        url_citation = ann.get("url_citation")
        if not isinstance(url_citation, dict):
            continue
        snippet = url_citation.get("content", "") or ""
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[: MAX_SNIPPET_CHARS - 3] + "..."
        results.append(
            {
                "title": url_citation.get("title", "No Title"),
                "url": url_citation.get("url", ""),
                "snippet": snippet,
            }
        )
    return results
