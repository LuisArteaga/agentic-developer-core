import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from orchestrator.config import (
    DEFAULT_MODEL,
    DEFAULT_ROUTING,
    FACTORY_JSON_PATH,
    _load_factory_config,
    resolve_model_config,
    get_chat_model_from_config,
)


class TestLoadFactoryConfig(unittest.TestCase):
    """Tests for _load_factory_config: missing, malformed, and valid files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_file_returns_empty_dict(self):
        """A non-existent factory.json yields an empty dict, never raises."""
        missing = self.temp_path / "nonexistent.json"
        result = _load_factory_config(missing)
        self.assertEqual(result, {})

    def test_malformed_json_returns_empty_dict(self):
        """A malformed JSON file yields an empty dict, never raises."""
        bad = self.temp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        result = _load_factory_config(bad)
        self.assertEqual(result, {})

    def test_non_object_json_returns_empty_dict(self):
        """A JSON array (not object) yields an empty dict."""
        arr = self.temp_path / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        result = _load_factory_config(arr)
        self.assertEqual(result, {})

    def test_valid_file_returns_parsed_dict(self):
        """A valid factory.json returns the parsed dict."""
        good = self.temp_path / "good.json"
        good.write_text(
            json.dumps({"plan": {"model": "test-model", "routing": ["X"]}}),
            encoding="utf-8",
        )
        result = _load_factory_config(good)
        self.assertEqual(result["plan"]["model"], "test-model")


class TestResolveModelConfig(unittest.TestCase):
    """Tests for resolve_model_config precedence and fallback."""

    def setUp(self):
        self.original_env = {}
        for var in [
            "AGENT_MODEL",
            "PLAN_MODEL",
            "TEST_WRITER_MODEL",
            "EXECUTE_MODEL",
        ]:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def _write_factory(self, content: dict) -> Path:
        """Write a temp factory.json and return its path."""
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(content, f)
        f.close()
        return Path(f.name)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_resolution_for_plan(self, mock_load):
        """resolve_model_config reads the plan entry from factory.json."""
        mock_load.return_value = {
            "plan": {
                "model": "deepseek/deepseek-v4-flash",
                "routing": ["DeepInfra", "SiliconFlow"],
                "temperature": 0.1,
            }
        }
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(cfg["routing"], ["DeepInfra", "SiliconFlow"])
        self.assertEqual(cfg["temperature"], 0.1)
        self.assertIsNone(cfg["options"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_resolution_with_options(self, mock_load):
        """The options field passes through when present in factory.json."""
        mock_load.return_value = {
            "execute": {
                "model": "moonshotai/kimi-k2.7-code",
                "routing": ["Together"],
                "temperature": 0.0,
                "options": {"thinking": "max"},
            }
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["options"], {"thinking": "max"})

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_env_override(self, mock_load):
        """PLAN_MODEL takes precedence over factory.json default."""
        mock_load.return_value = {
            "plan": {
                "model": "factory-plan-model",
                "routing": ["X"],
                "temperature": 0.5,
            }
        }
        os.environ["PLAN_MODEL"] = "env-plan-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "env-plan-model")
        self.assertIsNone(cfg["routing"])
        self.assertEqual(cfg["temperature"], 0.0)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_agent_model_fallback(self, mock_load):
        """AGENT_MODEL is the fallback for all nodes when no node-specific var is set."""
        mock_load.return_value = {
            "plan": {"model": "factory-plan-model", "routing": ["X"]}
        }
        os.environ["AGENT_MODEL"] = "agent-fallback-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "agent-fallback-model")
        self.assertIsNone(cfg["routing"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_overrides_agent_model(self, mock_load):
        """Node-specific var wins over AGENT_MODEL."""
        mock_load.return_value = {}
        os.environ["AGENT_MODEL"] = "agent-model"
        os.environ["PLAN_MODEL"] = "plan-specific"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "plan-specific")

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_inherits_temperature_when_model_matches(self, mock_load):
        """When the override model matches the factory entry, temperature/options are inherited."""
        mock_load.return_value = {
            "plan": {
                "model": "shared-model",
                "routing": ["X"],
                "temperature": 0.7,
                "options": {"thinking": "high"},
            }
        }
        os.environ["PLAN_MODEL"] = "shared-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "shared-model")
        self.assertIsNone(cfg["routing"])
        self.assertEqual(cfg["temperature"], 0.7)
        self.assertEqual(cfg["options"], {"thinking": "high"})

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_defaults_when_model_differs(self, mock_load):
        """When the override model differs from factory, temperature=0.0 and options=None."""
        mock_load.return_value = {
            "plan": {
                "model": "factory-model",
                "routing": ["X"],
                "temperature": 0.9,
                "options": {"thinking": "max"},
            }
        }
        os.environ["PLAN_MODEL"] = "different-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "different-model")
        self.assertEqual(cfg["temperature"], 0.0)
        self.assertIsNone(cfg["options"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_absent_from_factory_falls_back_to_default(self, mock_load):
        """A node not listed in a valid factory.json falls back to DEFAULT_MODEL."""
        mock_load.return_value = {"plan": {"model": "x"}}
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertEqual(cfg["routing"], DEFAULT_ROUTING["execute"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_missing_factory_falls_back_to_default(self, mock_load):
        """A missing/malformed factory (empty dict) falls back to DEFAULT_MODEL."""
        mock_load.return_value = {}
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertEqual(cfg["routing"], DEFAULT_ROUTING["plan"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_non_dict_factory_entry_treated_as_absent(self, mock_load):
        """A factory entry that is not a dict (e.g. a string) is treated as absent."""
        mock_load.return_value = {"plan": "not-a-dict"}
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertEqual(cfg["routing"], DEFAULT_ROUTING["plan"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_entry_without_temperature_defaults_to_zero(self, mock_load):
        """A factory entry missing 'temperature' defaults to 0.0."""
        mock_load.return_value = {"plan": {"model": "x", "routing": ["Y"]}}
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["temperature"], 0.0)


class TestResolveJudgeConfigs(unittest.TestCase):
    """Tests for the 4 PR-review judge entries in config/factory.json (issue #37)."""

    def setUp(self):
        self.original_env = {}
        for var in [
            "AGENT_MODEL",
            "SYNTAX_LINT_MODEL",
            "TEST_COVERAGE_MODEL",
            "ARCHITECTURE_MODEL",
            "SECURITY_MODEL",
        ]:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def test_resolve_judge_configs(self):
        """Each judge resolves its model/routing/temperature from factory.json; security has options."""
        syntax = resolve_model_config("syntax_lint")
        self.assertEqual(syntax["model"], "moonshotai/kimi-k2.7-code")
        self.assertEqual(
            syntax["routing"], ["Together", "SiliconFlow", "MoonshotAI", "Inceptron"]
        )
        self.assertEqual(syntax["temperature"], 0.0)
        self.assertIsNone(syntax["options"])

        test_cov = resolve_model_config("test_coverage")
        self.assertEqual(test_cov["model"], "moonshotai/kimi-k2.7-code")
        self.assertEqual(test_cov["temperature"], 0.0)

        arch = resolve_model_config("architecture")
        self.assertEqual(arch["model"], "z-ai/glm-5.2")
        self.assertEqual(
            arch["routing"],
            ["Together", "DeepInfra", "Fireworks", "Parasail", "Inceptron"],
        )

        sec = resolve_model_config("security")
        self.assertEqual(sec["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(
            sec["routing"],
            ["DeepInfra", "SiliconFlow", "Novita", "Parasail", "DeepSeek"],
        )
        self.assertEqual(sec["options"], {"thinking": "max"})

    def test_judge_env_override_disables_routing(self):
        """A node-specific env override disables routing and resets temperature to 0.0."""
        os.environ["SYNTAX_LINT_MODEL"] = "foo"
        cfg = resolve_model_config("syntax_lint")
        self.assertEqual(cfg["model"], "foo")
        self.assertIsNone(cfg["routing"])
        self.assertEqual(cfg["temperature"], 0.0)


class TestGetChatModelFromConfig(unittest.TestCase):
    """Tests for get_chat_model_from_config construction and extra_body wiring."""

    def setUp(self):
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"

    def tearDown(self):
        if self.original_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self.original_api_key
        elif "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]

    def test_constructs_chat_openai_with_model(self):
        """get_chat_model_from_config returns a ChatOpenAI with the given model."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        self.assertEqual(llm.model_name, "test-model")

    def test_api_key_wrapped_in_secret_str(self):
        """The API key is wrapped in pydantic SecretStr to prevent accidental exposure."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        from pydantic import SecretStr

        api_key = llm.openai_api_key
        assert isinstance(api_key, SecretStr)
        self.assertEqual(api_key.get_secret_value(), "mock-key")

    def test_routing_passed_via_extra_body(self):
        """The routing list is lowercased and passed to OpenRouter via extra_body provider."""
        cfg = {
            "model": "test-model",
            "routing": ["DeepInfra", "SiliconFlow"],
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        extra_body = llm.extra_body or {}
        self.assertIn("provider", extra_body)
        self.assertEqual(extra_body["provider"]["order"], ["deepinfra", "siliconflow"])
        self.assertFalse(extra_body["provider"]["allow_fallbacks"])

    def test_no_routing_means_no_provider_in_extra_body(self):
        """When routing is None (env override), no provider key appears in extra_body."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        extra_body = llm.extra_body or {}
        self.assertNotIn("provider", extra_body)

    def test_options_merged_into_extra_body(self):
        """The options dict is merged into extra_body alongside provider routing."""
        cfg = {
            "model": "test-model",
            "routing": ["DeepInfra"],
            "temperature": 0.0,
            "options": {"thinking": "max"},
        }
        llm = get_chat_model_from_config(cfg)
        extra_body = llm.extra_body or {}
        self.assertEqual(extra_body["thinking"], "max")
        self.assertIn("provider", extra_body)

    def test_temperature_passed_through(self):
        """The temperature from the config reaches the ChatOpenAI instance."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.5,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        self.assertEqual(llm.temperature, 0.5)

    def test_max_retries_set(self):
        """The LLM client has max_retries set for transient API error resilience."""
        from orchestrator.config import LLM_MAX_RETRIES

        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        self.assertEqual(llm.max_retries, LLM_MAX_RETRIES)

    def test_timeout_set(self):
        """The LLM client has a request_timeout to prevent indefinite hangs on OpenRouter."""
        from orchestrator.config import LLM_TIMEOUT

        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        llm = get_chat_model_from_config(cfg)
        self.assertEqual(llm.request_timeout, LLM_TIMEOUT)

    def test_missing_api_key_raises(self):
        """A missing OPENROUTER_API_KEY raises ValueError before construction."""
        del os.environ["OPENROUTER_API_KEY"]
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
        }
        with self.assertRaises(ValueError) as ctx:
            get_chat_model_from_config(cfg)
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))


class TestRealFactoryJson(unittest.TestCase):
    """Integration: resolve against the actual config/factory.json shipped with the repo."""

    def setUp(self):
        self.original_env = {}
        for var in ["AGENT_MODEL", "PLAN_MODEL", "TEST_WRITER_MODEL", "EXECUTE_MODEL"]:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def test_factory_json_exists(self):
        """The shipped config/factory.json exists at the expected path."""
        self.assertTrue(FACTORY_JSON_PATH.exists(), f"{FACTORY_JSON_PATH} should exist")

    def test_plan_uses_flash(self):
        """The plan node resolves to deepseek-v4-flash in the shipped factory.json."""
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "deepseek/deepseek-v4-flash")

    def test_execute_uses_kimi(self):
        """The execute node resolves to kimi-k2.7-code in the shipped factory.json."""
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["model"], "moonshotai/kimi-k2.7-code")

    def test_test_writer_uses_kimi(self):
        """The test_writer node resolves to kimi-k2.7-code in the shipped factory.json."""
        cfg = resolve_model_config("test_writer")
        self.assertEqual(cfg["model"], "moonshotai/kimi-k2.7-code")

    def test_all_nodes_have_routing(self):
        """Every node in the shipped factory.json has a non-empty routing list."""
        for node in ["plan", "test_writer", "execute"]:
            cfg = resolve_model_config(node)
            self.assertIsNotNone(cfg["routing"], f"{node} should have routing")
            self.assertGreater(
                len(cfg["routing"]), 0, f"{node} routing should be non-empty"
            )

    def test_default_routing_covers_all_nodes(self):
        """The DEFAULT_ROUTING fallback map covers every known node name."""
        for node in ["plan", "test_writer", "execute"]:
            self.assertIn(node, DEFAULT_ROUTING, f"{node} missing from DEFAULT_ROUTING")
            self.assertGreater(len(DEFAULT_ROUTING[node]), 0)


if __name__ == "__main__":
    unittest.main()
