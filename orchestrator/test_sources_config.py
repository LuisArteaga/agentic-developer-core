import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from orchestrator.sources_config import (
    SearchParametersConfig,
    SourcesConfig,
    load_sources_config,
    reset_sources_config_cache,
)


class TestSearchParametersConfig(unittest.TestCase):
    def test_defaults(self):
        sp = SearchParametersConfig()
        self.assertEqual(sp.engine, "auto")
        self.assertIsNone(sp.search_context_size)
        self.assertIsNone(sp.max_results)
        self.assertIsNone(sp.max_total_results)
        self.assertEqual(sp.excluded_domains, [])

    def test_all_fields_set(self):
        sp = SearchParametersConfig(
            engine="exa",
            search_context_size="high",
            max_results=5,
            max_total_results=15,
            excluded_domains=["reddit.com"],
        )
        self.assertEqual(sp.engine, "exa")
        self.assertEqual(sp.search_context_size, "high")
        self.assertEqual(sp.max_results, 5)
        self.assertEqual(sp.max_total_results, 15)
        self.assertEqual(sp.excluded_domains, ["reddit.com"])


class TestSourcesConfigValidation(unittest.TestCase):
    def test_default_is_non_strict_and_valid(self):
        """Default SourcesConfig is non-strict with empty urls/domains — valid."""
        cfg = SourcesConfig()
        self.assertFalse(cfg.strict)
        self.assertEqual(cfg.urls, [])
        self.assertEqual(cfg.domains, [])

    def test_strict_with_domains_is_valid(self):
        cfg = SourcesConfig(strict=True, domains=["arxiv.org", "pypi.org"])
        self.assertTrue(cfg.strict)
        self.assertEqual(cfg.domains, ["arxiv.org", "pypi.org"])

    def test_strict_with_urls_only_is_valid(self):
        cfg = SourcesConfig(
            strict=True, urls=["https://github.com/langchain-ai/langgraph"]
        )
        self.assertTrue(cfg.strict)
        self.assertEqual(len(cfg.urls), 1)

    def test_strict_with_no_sources_raises(self):
        """Strict mode enabled but urls and domains both empty -> ValueError."""
        with self.assertRaises(ValidationError) as ctx:
            SourcesConfig(strict=True)
        self.assertIn("At least one source must be defined", str(ctx.exception))

    def test_non_strict_with_no_sources_is_valid(self):
        cfg = SourcesConfig(strict=False)
        self.assertFalse(cfg.strict)

    def test_excluded_domains_normalized_to_lower(self):
        cfg = SourcesConfig(
            strict=False,
            domains=["Arxiv.Org"],
            search=SearchParametersConfig(excluded_domains=["  Reddit.COM  ", ""]),
        )
        self.assertEqual(cfg.search.excluded_domains, ["reddit.com"])

    def test_excluded_domains_overlap_with_allowlist_warns(self):
        """A domain in both allowlist and excluded_domains logs a warning."""
        with self.assertLogs("orchestrator.sources_config", level="WARNING") as cm:
            SourcesConfig(
                strict=True,
                domains=["arxiv.org"],
                search=SearchParametersConfig(excluded_domains=["arxiv.org"]),
            )
        self.assertTrue(
            any("Overlap detected" in msg for msg in cm.output),
            f"expected overlap warning, got {cm.output}",
        )

    def test_search_defaults_to_auto_when_absent(self):
        cfg = SourcesConfig.model_validate({"strict": False})
        self.assertEqual(cfg.search.engine, "auto")


class TestLoadSourcesConfig(unittest.TestCase):
    def setUp(self):
        # The loader caches module-level; isolate every test.
        reset_sources_config_cache()

    def tearDown(self):
        reset_sources_config_cache()

    def _write(self, tmpdir: str, content: str) -> Path:
        p = Path(tmpdir) / "sources.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_missing_file_falls_back_to_defaults(self):
        """A missing sources.toml falls back to non-strict defaults; no crash."""
        with self.assertLogs("orchestrator.sources_config", level="WARNING") as cm:
            cfg = load_sources_config(path=Path("/nonexistent/sources.toml"))
        self.assertFalse(cfg.strict)
        self.assertEqual(cfg.search.engine, "auto")
        self.assertTrue(any("not found" in m for m in cm.output))

    def test_malformed_toml_falls_back_to_defaults(self):
        """Malformed TOML degrades to defaults with a warning; no crash."""
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "strict = = = not toml\n  - [unclosed")
            with self.assertLogs("orchestrator.sources_config", level="WARNING") as cm:
                cfg = load_sources_config(path=p)
        self.assertFalse(cfg.strict)
        self.assertTrue(any("malformed" in m for m in cm.output))

    def test_valid_non_strict_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(
                d,
                "strict = false\n[search]\nengine = 'exa'\nmax_results = 3\n",
            )
            cfg = load_sources_config(path=p)
        self.assertFalse(cfg.strict)
        self.assertEqual(cfg.search.engine, "exa")
        self.assertEqual(cfg.search.max_results, 3)

    def test_valid_strict_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(
                d,
                'strict = true\ndomains = ["arxiv.org", "pypi.org"]\n',
            )
            cfg = load_sources_config(path=p)
        self.assertTrue(cfg.strict)
        self.assertEqual(cfg.domains, ["arxiv.org", "pypi.org"])

    def test_strict_no_sources_raises_at_load(self):
        """A present sources.toml with strict=true and no domains -> ValueError
        propagates (not silently degraded)."""
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "strict = true\n")
            with self.assertRaises(ValueError):
                load_sources_config(path=p)

    def test_cache_returns_same_instance(self):
        """load_sources_config caches; a second call returns the same object."""
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "strict = false\n")
            first = load_sources_config(path=p)
            # Second call ignores the path argument because the cache is hot.
            second = load_sources_config(path=Path("/nonexistent/ignored.toml"))
        self.assertIs(first, second)

    def test_reset_cache_forces_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "strict = false\n[search]\nengine = 'exa'\n")
            first = load_sources_config(path=p)
            reset_sources_config_cache()
            second = load_sources_config(path=p)
        self.assertIsNot(first, second)
        self.assertEqual(second.search.engine, "exa")

    @patch("orchestrator.sources_config._sources_cache", None)
    def test_project_default_sources_toml_is_non_strict(self):
        """The shipped config/sources.toml is a valid non-strict default."""
        reset_sources_config_cache()
        cfg = load_sources_config()
        self.assertFalse(cfg.strict)
        self.assertEqual(cfg.search.engine, "auto")


if __name__ == "__main__":
    unittest.main()
