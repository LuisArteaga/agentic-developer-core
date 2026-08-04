"""Pydantic schemas and cached loader for ``config/sources.toml``.

Ported from agentic-planner-core's ``SourcesConfig`` / ``SearchParametersConfig``
(planner/config.py, which uses YAML). We deviate to TOML to keep the
dependency surface minimal: ``tomllib`` is stdlib in Python 3.11+ and this
project requires ``>=3.12``, so no third-party dependency is needed to parse
the config (ADR-0017 minimal-dependency philosophy). The Pydantic schema is
unchanged from the YAML port.

Defines optional domain allowlisting and OpenRouter ``web_search`` tool
parameters. Shared by the Web Search and URL Fetch Worker Tools (issues #38,
#39).
"""

import logging
import tomllib
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("orchestrator.sources_config")

# Path resolution: config/sources.toml relative to the project root
# (the parent of this module's package directory).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES_TOML_PATH = _PROJECT_ROOT / "config" / "sources.toml"


class SearchParametersConfig(BaseModel):
    """Pydantic schema for the OpenRouter ``web_search`` tool parameters."""

    engine: str = "auto"
    search_context_size: Optional[str] = None
    max_results: Optional[int] = None
    max_total_results: Optional[int] = None
    excluded_domains: List[str] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    """Pydantic schema for parsing and validating ``config/sources.yaml``.

    Under ``strict`` mode, search/fetch is restricted to the configured
    ``urls`` and ``domains``. At least one of those must be non-empty when
    strict is enabled, enforced by the validator below.
    """

    strict: bool = False
    urls: List[str] = Field(default_factory=list)
    domains: List[str] = Field(default_factory=list)
    search: SearchParametersConfig = Field(default_factory=SearchParametersConfig)

    @model_validator(mode="after")
    def _validate_strict_sources(self) -> "SourcesConfig":
        if self.strict and not self.urls and not self.domains:
            raise ValueError(
                "Strict-mode is enabled (strict: true), but both 'urls' "
                "and 'domains' are empty. At least one source must be defined."
            )

        # Normalize excluded_domains to lower-case and warn on overlap with
        # the allowlisted domains (a domain cannot be both allowed and excluded).
        if self.search and self.search.excluded_domains:
            normalized = [
                d.strip().lower()
                for d in self.search.excluded_domains
                if d and d.strip()
            ]
            self.search.excluded_domains = normalized

            whitelisted = {d.strip().lower() for d in self.domains if d}
            for ext in self.search.excluded_domains:
                if ext in whitelisted:
                    logger.warning(
                        "Overlap detected: Domain '%s' is defined in both "
                        "allowed sources and excluded_domains.",
                        ext,
                    )
        return self


# Module-level cache: sources.yaml is loaded once at orchestrator startup and
# re-used across tool calls (cross-cutting concern from issue #38). Re-reading a
# small YAML file per tool call would be wasteful and would re-trigger Pydantic
# validation repeatedly.
_sources_cache: Optional[SourcesConfig] = None


def load_sources_config(path: Path = SOURCES_TOML_PATH) -> SourcesConfig:
    """Load, validate, and cache ``config/sources.toml``.

    Behavior:
      * **Missing file** — fall back to default search parameters (engine:
        ``auto``, non-strict, no domain restrictions) with a warning log.
        Does not crash the orchestrator.
      * **Malformed TOML** — fall back to defaults with a warning log.
      * **Present but invalid** (e.g. strict mode with no domains) — let
        Pydantic raise ``ValueError`` at config load time. A present but
        misconfigured file is a configuration error that must surface, not
        silently degrade. This matches the planner-core port (issue #38 edge
        case: "strict mode enabled but no domains configured — raise
        ValueError at config load time").

    The resolved config is cached module-level; subsequent calls return the
    cached instance without re-reading the file.
    """
    global _sources_cache
    if _sources_cache is not None:
        return _sources_cache

    if not path.exists():
        logger.warning(
            "Sources configuration not found at %s; falling back to default "
            "search parameters (engine: auto, no domain restrictions).",
            path,
        )
        _sources_cache = SourcesConfig()
        return _sources_cache

    try:
        # tomllib.load requires a binary file handle.
        with open(path, "rb") as f:
            data = tomllib.load(f) or {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(
            "Sources configuration at %s is malformed (%s); falling back "
            "to default search parameters.",
            path,
            e,
        )
        _sources_cache = SourcesConfig()
        return _sources_cache

    # Let Pydantic validation (incl. the strict-mode check) raise on a present
    # but invalid file. Only a missing/malformed file degrades to defaults.
    _sources_cache = SourcesConfig.model_validate(data)
    return _sources_cache


def reset_sources_config_cache() -> None:
    """Clear the cached ``SourcesConfig``.

    Test helper: tests that swap ``SOURCES_YAML_PATH`` or write alternate
    configs must reset the cache so the next ``load_sources_config`` re-reads.
    """
    global _sources_cache
    _sources_cache = None
