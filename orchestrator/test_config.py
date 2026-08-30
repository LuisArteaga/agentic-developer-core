import json
import logging
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from orchestrator.config import (
    DEFAULT_LOOP_HARD_LIMIT,
    DEFAULT_LOOP_WARN_THRESHOLD,
    DEFAULT_LOOP_WINDOW_SIZE,
    DEFAULT_MODEL,
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_ROUTING,
    FACTORY_JSON_PATH,
    _load_factory_config,
    get_chat_model_from_config,
    resolve_model_config,
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
            "SYNTAX_LINT_MODEL",
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
    def test_factory_resolution_passes_max_tokens(self, mock_load):
        """max_tokens from factory.json is passed through in the factory path."""
        mock_load.return_value = {
            "plan": {
                "model": "deepseek/deepseek-v4-flash",
                "routing": ["DeepInfra"],
                "temperature": 0.0,
                "max_tokens": 8192,
            }
        }
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["max_tokens"], 8192)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_resolution_no_max_tokens_defaults_none(self, mock_load):
        """Absent max_tokens in factory.json resolves to None."""
        mock_load.return_value = {
            "plan": {
                "model": "deepseek/deepseek-v4-flash",
                "routing": ["DeepInfra"],
                "temperature": 0.0,
            }
        }
        cfg = resolve_model_config("plan")
        self.assertIsNone(cfg["max_tokens"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_inherits_max_tokens_when_model_matches(self, mock_load):
        """When the override model matches the factory entry, max_tokens is inherited."""
        mock_load.return_value = {
            "plan": {
                "model": "shared-model",
                "routing": ["X"],
                "temperature": 0.7,
                "max_tokens": 4096,
            }
        }
        os.environ["PLAN_MODEL"] = "shared-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["max_tokens"], 4096)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_max_tokens_none_when_model_differs(self, mock_load):
        """When the override model differs from factory, max_tokens is None."""
        mock_load.return_value = {
            "plan": {
                "model": "factory-model",
                "routing": ["X"],
                "temperature": 0.9,
                "max_tokens": 8192,
            }
        }
        os.environ["PLAN_MODEL"] = "different-model"
        cfg = resolve_model_config("plan")
        self.assertIsNone(cfg["max_tokens"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_hardcoded_fallback_max_tokens_is_none(self, mock_load):
        """Hardcoded default path has max_tokens=None."""
        mock_load.return_value = {}
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertIsNone(cfg["max_tokens"])

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

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_resolution_returns_fallback_model(self, mock_load):
        """fallback_model from factory.json is passed through in the factory path."""
        mock_load.return_value = {
            "syntax_lint": {
                "model": "moonshotai/kimi-k2.7-code",
                "routing": ["Together"],
                "temperature": 0.0,
                "fallback_model": "z-ai/glm-5.2",
            }
        }
        cfg = resolve_model_config("syntax_lint")
        self.assertEqual(cfg["fallback_model"], "z-ai/glm-5.2")

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_resolution_no_fallback_model_defaults_none(self, mock_load):
        """Absent fallback_model in factory.json resolves to None."""
        mock_load.return_value = {
            "plan": {
                "model": "deepseek/deepseek-v4-flash",
                "routing": ["DeepInfra"],
                "temperature": 0.0,
            }
        }
        cfg = resolve_model_config("plan")
        self.assertIsNone(cfg["fallback_model"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_returns_fallback_model_from_factory(self, mock_load):
        """Env override still reads fallback_model from the factory entry."""
        mock_load.return_value = {
            "syntax_lint": {
                "model": "moonshotai/kimi-k2.7-code",
                "routing": ["Together"],
                "temperature": 0.0,
                "fallback_model": "z-ai/glm-5.2",
            }
        }
        os.environ["SYNTAX_LINT_MODEL"] = "env-override-model"
        cfg = resolve_model_config("syntax_lint")
        self.assertEqual(cfg["model"], "env-override-model")
        self.assertIsNone(cfg["routing"])
        self.assertEqual(cfg["fallback_model"], "z-ai/glm-5.2")

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_no_factory_fallback_model_is_none(self, mock_load):
        """Env override with absent factory entry -> fallback_model is None."""
        mock_load.return_value = {}
        os.environ["PLAN_MODEL"] = "env-model"
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "env-model")
        self.assertIsNone(cfg["fallback_model"])

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_absent_falls_back_to_default_no_fallback_model(self, mock_load):
        """Hardcoded default path has fallback_model=None."""
        mock_load.return_value = {"plan": {"model": "x"}}
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertIsNone(cfg["fallback_model"])


class TestResolveRecursionLimit(unittest.TestCase):
    """Tests for the per-node Worker Recursion Budget resolution (ADR-0045)."""

    # Env vars that can override recursion_limit (and the model env touched by
    # the env-override-path test); isolated per-test.
    _ENV_VARS = [
        "AGENT_RECURSION_LIMIT",
        "EXECUTE_RECURSION_LIMIT",
        "TEST_WRITER_RECURSION_LIMIT",
        "PLAN_RECURSION_LIMIT",
        "EXECUTE_MODEL",
    ]

    def setUp(self):
        self.original_env = {}
        for var in self._ENV_VARS:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_default_when_factory_has_no_recursion_limit(self, mock_load):
        """Absent recursion_limit in factory.json resolves to DEFAULT_RECURSION_LIMIT."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "temperature": 0.0}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], DEFAULT_RECURSION_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_default_on_hardcoded_fallback_path(self, mock_load):
        """Hardcoded fallback path (node absent) still resolves recursion_limit."""
        mock_load.return_value = {}
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], DEFAULT_RECURSION_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_recursion_limit_honored(self, mock_load):
        """factory.json recursion_limit is passed through in the factory path."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 99}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 99)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_env_overrides_factory(self, mock_load):
        """EXECUTE_RECURSION_LIMIT takes precedence over factory.json."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 99}
        }
        os.environ["EXECUTE_RECURSION_LIMIT"] = "7"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 7)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_agent_recursion_limit_general_fallback(self, mock_load):
        """AGENT_RECURSION_LIMIT applies when no node-specific var is set."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 99}
        }
        os.environ["AGENT_RECURSION_LIMIT"] = "42"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 42)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_overrides_agent_recursion_limit(self, mock_load):
        """Node-specific var wins over AGENT_RECURSION_LIMIT."""
        mock_load.return_value = {}
        os.environ["AGENT_RECURSION_LIMIT"] = "42"
        os.environ["EXECUTE_RECURSION_LIMIT"] = "11"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 11)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_invalid_env_falls_back_to_factory(self, mock_load):
        """A malformed env value is ignored, falling back to factory.json."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 80}
        }
        os.environ["EXECUTE_RECURSION_LIMIT"] = "not-a-number"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 80)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_invalid_env_falls_back_to_default(self, mock_load):
        """A malformed env value with no factory entry falls back to default."""
        mock_load.return_value = {"execute": {"model": "m", "routing": ["X"]}}
        os.environ["EXECUTE_RECURSION_LIMIT"] = "not-a-number"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], DEFAULT_RECURSION_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_invalid_factory_recursion_limit_falls_back_to_default(self, mock_load):
        """A malformed recursion_limit in factory.json is ignored, falling back
        to DEFAULT_RECURSION_LIMIT (ADR-0045)."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": "bad"}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], DEFAULT_RECURSION_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_distinct_budgets_per_node(self, mock_load):
        """Execute and test_writer can carry distinct recursion_limits (ADR-0045)."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 60},
            "test_writer": {"model": "m", "routing": ["X"], "recursion_limit": 40},
        }
        self.assertEqual(resolve_model_config("execute")["recursion_limit"], 60)
        self.assertEqual(resolve_model_config("test_writer")["recursion_limit"], 40)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_path_still_resolves_recursion_limit(self, mock_load):
        """The model env-override path also returns a recursion_limit."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "recursion_limit": 55}
        }
        os.environ["EXECUTE_MODEL"] = "other-model"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], 55)


class TestProductionFactoryRecursionBudget(unittest.TestCase):
    """Guard: the production config/factory.json carries explicit per-node
    Recursion Budgets for the ReAct worker nodes (issue #143).

    Unlike TestResolveRecursionLimit, these tests are deliberately unmocked:
    they resolve through the public ``resolve_model_config`` against the real
    factory file, so accidentally dropping an explicit ``recursion_limit``
    (silently reverting a node to DEFAULT_RECURSION_LIMIT) fails here. The
    assertions are value-agnostic — re-sizing from measured Trajectory Length
    data (ADR-0029) must never require touching this test.
    """

    _ENV_VARS = [
        "AGENT_RECURSION_LIMIT",
        "EXECUTE_RECURSION_LIMIT",
        "TEST_WRITER_RECURSION_LIMIT",
        "PLAN_RECURSION_LIMIT",
        "EXECUTE_MODEL",
        "TEST_WRITER_MODEL",
    ]

    def setUp(self):
        self.original_env = {}
        for var in self._ENV_VARS:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def _raw_node_config(self, node: str) -> dict:
        self.assertTrue(FACTORY_JSON_PATH.exists(), f"{FACTORY_JSON_PATH} missing")
        with open(FACTORY_JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn(node, raw, f"production factory.json lacks node '{node}'")
        self.assertIn(
            "recursion_limit",
            raw[node],
            f"node '{node}' has no explicit recursion_limit; "
            "it would silently fall back to DEFAULT_RECURSION_LIMIT",
        )
        return raw[node]

    def test_execute_carries_explicit_recursion_budget(self):
        """execute's budget is explicit in factory.json and resolves verbatim."""
        node_cfg = self._raw_node_config("execute")
        self.assertIsInstance(node_cfg["recursion_limit"], int)
        self.assertGreater(node_cfg["recursion_limit"], 0)
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["recursion_limit"], node_cfg["recursion_limit"])

    def test_test_writer_carries_explicit_recursion_budget(self):
        """test_writer's budget is explicit in factory.json and resolves verbatim."""
        node_cfg = self._raw_node_config("test_writer")
        self.assertIsInstance(node_cfg["recursion_limit"], int)
        self.assertGreater(node_cfg["recursion_limit"], 0)
        cfg = resolve_model_config("test_writer")
        self.assertEqual(cfg["recursion_limit"], node_cfg["recursion_limit"])


class TestResolveLoopConfig(unittest.TestCase):
    """Tests for the per-node Tool Loop Detection config (issue #121 / ADR-0046)."""

    _ENV_VARS = [
        "AGENT_LOOP_WARN_THRESHOLD",
        "AGENT_LOOP_HARD_LIMIT",
        "AGENT_LOOP_WINDOW_SIZE",
        "EXECUTE_LOOP_WARN_THRESHOLD",
        "EXECUTE_LOOP_HARD_LIMIT",
        "EXECUTE_LOOP_WINDOW_SIZE",
        "TEST_WRITER_LOOP_HARD_LIMIT",
        "EXECUTE_MODEL",
    ]

    def setUp(self):
        self.original_env = {}
        for var in self._ENV_VARS:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def _loop(self, cfg):
        return (
            cfg["loop_warn_threshold"],
            cfg["loop_hard_limit"],
            cfg["loop_window_size"],
        )

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_defaults_when_factory_has_no_loop_keys(self, mock_load):
        """Absent loop keys resolve to the defaults."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "temperature": 0.0}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(
            self._loop(cfg),
            (
                DEFAULT_LOOP_WARN_THRESHOLD,
                DEFAULT_LOOP_HARD_LIMIT,
                DEFAULT_LOOP_WINDOW_SIZE,
            ),
        )

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_defaults_on_hardcoded_fallback_path(self, mock_load):
        """Hardcoded fallback path (node absent) still resolves loop config."""
        mock_load.return_value = {}
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], DEFAULT_LOOP_HARD_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_factory_loop_keys_honored(self, mock_load):
        """factory.json loop keys are passed through in the factory path."""
        mock_load.return_value = {
            "execute": {
                "model": "m",
                "routing": ["X"],
                "loop_warn_threshold": 2,
                "loop_hard_limit": 4,
                "loop_window_size": 8,
            }
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(self._loop(cfg), (2, 4, 8))

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_env_overrides_factory(self, mock_load):
        """EXECUTE_LOOP_HARD_LIMIT takes precedence over factory.json."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 9}
        }
        os.environ["EXECUTE_LOOP_HARD_LIMIT"] = "3"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 3)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_agent_general_env_fallback(self, mock_load):
        """AGENT_LOOP_HARD_LIMIT applies when no node-specific var is set."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 9}
        }
        os.environ["AGENT_LOOP_HARD_LIMIT"] = "7"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 7)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_node_specific_overrides_agent_env(self, mock_load):
        """Node-specific var wins over AGENT_LOOP_*."""
        mock_load.return_value = {}
        os.environ["AGENT_LOOP_HARD_LIMIT"] = "7"
        os.environ["EXECUTE_LOOP_HARD_LIMIT"] = "4"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 4)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_invalid_env_falls_back_to_factory(self, mock_load):
        """A malformed env value is ignored, falling back to factory.json."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 6}
        }
        os.environ["EXECUTE_LOOP_HARD_LIMIT"] = "not-a-number"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 6)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_invalid_factory_value_falls_back_to_default(self, mock_load):
        """A malformed loop_hard_limit in factory.json is ignored."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": "bad"}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], DEFAULT_LOOP_HARD_LIMIT)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_distinct_loop_config_per_node(self, mock_load):
        """Execute and test_writer can carry distinct loop config."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 8},
            "test_writer": {"model": "m", "routing": ["X"], "loop_hard_limit": 3},
        }
        self.assertEqual(resolve_model_config("execute")["loop_hard_limit"], 8)
        self.assertEqual(resolve_model_config("test_writer")["loop_hard_limit"], 3)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_env_override_path_still_resolves_loop_config(self, mock_load):
        """The model env-override path also returns loop config."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 6}
        }
        os.environ["EXECUTE_MODEL"] = "other-model"
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 6)

    @unittest.mock.patch("orchestrator.config._load_factory_config")
    def test_hard_limit_zero_disables(self, mock_load):
        """loop_hard_limit=0 is a valid disabling sentinel (resolved, not crashing)."""
        mock_load.return_value = {
            "execute": {"model": "m", "routing": ["X"], "loop_hard_limit": 0}
        }
        cfg = resolve_model_config("execute")
        self.assertEqual(cfg["loop_hard_limit"], 0)


class TestResolveJudgeConfigs(unittest.TestCase):
    """Tests for the 4 PR-review judge entries in config/factory.json (issue #37)."""

    GLM_JUDGE_ROUTING = [
        "Z.AI",
        "Novita",
        "DeepInfra",
        "Modal",
        "Fireworks",
        "Friendli",
        "Parasail",
        "Phala",
    ]

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
        """Each judge resolves its model/routing/temperature from factory.json.

        All four judges carry a pinned provider order (ADR-0021 amendment
        2026-09-01: the GLM judges re-pin from the price-weighted default to
        a wide-curated fp8 list — quality control without the two-provider
        death pool; security's kimi-k3 order is unchanged).
        """
        syntax = resolve_model_config("syntax_lint")
        self.assertEqual(syntax["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(syntax["routing"], self.GLM_JUDGE_ROUTING)
        self.assertEqual(syntax["temperature"], 0.0)
        self.assertIsNone(syntax["options"])

        test_cov = resolve_model_config("test_coverage")
        self.assertEqual(test_cov["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(test_cov["routing"], self.GLM_JUDGE_ROUTING)
        self.assertEqual(test_cov["temperature"], 0.0)

        arch = resolve_model_config("architecture")
        self.assertEqual(arch["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(arch["routing"], self.GLM_JUDGE_ROUTING)

        sec = resolve_model_config("security")
        self.assertEqual(sec["model"], "moonshotai/kimi-k3")
        self.assertEqual(
            sec["routing"],
            ["Together", "Fireworks", "Parasail", "Moonshot AI", "DeepInfra"],
        )
        self.assertEqual(sec["options"], {"reasoning": {"effort": "high"}})

    def test_resolve_judge_fallback_models(self):
        """Each judge resolves its fallback_model from factory.json (ADR-0021)."""
        syntax = resolve_model_config("syntax_lint")
        self.assertEqual(syntax["fallback_model"], "z-ai/glm-5.2")

        test_cov = resolve_model_config("test_coverage")
        self.assertEqual(test_cov["fallback_model"], "z-ai/glm-5.2")

        arch = resolve_model_config("architecture")
        self.assertEqual(arch["fallback_model"], "moonshotai/kimi-k3")

        sec = resolve_model_config("security")
        self.assertEqual(sec["fallback_model"], "z-ai/glm-5.2")

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

    def test_reasoning_effort_merged_into_extra_body(self):
        """Nested reasoning options (ADR-0051) pass through to extra_body verbatim."""
        cfg = {
            "model": "test-model",
            "routing": ["DeepInfra"],
            "temperature": 0.0,
            "options": {"reasoning": {"effort": "low"}},
        }
        llm = get_chat_model_from_config(cfg)
        extra_body = llm.extra_body or {}
        self.assertEqual(extra_body["reasoning"], {"effort": "low"})
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

    def test_max_tokens_passed_through(self):
        """max_tokens from the config reaches the ChatOpenAI instance."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
            "max_tokens": 8192,
        }
        llm = get_chat_model_from_config(cfg)
        self.assertEqual(llm.max_tokens, 8192)

    def test_max_tokens_none_not_passed(self):
        """When max_tokens is None, the constructor uses the model/provider default."""
        cfg = {
            "model": "test-model",
            "routing": None,
            "temperature": 0.0,
            "options": None,
            "max_tokens": None,
        }
        llm = get_chat_model_from_config(cfg)
        # ChatOpenAI defaults max_tokens to NotGiven (not set) when not passed.
        self.assertNotEqual(llm.max_tokens, 8192)

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
        """The plan node resolves to deepseek-v4-flash-0731 in the shipped factory.json."""
        cfg = resolve_model_config("plan")
        self.assertEqual(cfg["model"], "deepseek/deepseek-v4-flash-0731")

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

    def test_plan_has_max_tokens(self):
        """The plan node has explicit max_tokens in the shipped factory.json."""
        cfg = resolve_model_config("plan")
        self.assertIsNotNone(cfg["max_tokens"])
        self.assertGreater(cfg["max_tokens"], 0)

    def test_bin_eval_has_max_tokens(self):
        """The bin_eval node has explicit max_tokens in the shipped factory.json."""
        cfg = resolve_model_config("bin_eval")
        self.assertIsNotNone(cfg["max_tokens"])
        self.assertGreater(cfg["max_tokens"], 0)

    def test_bin_eval_reasoning_effort_low(self):
        """The bin_eval node caps reasoning effort to low in the shipped factory.json (ADR-0051)."""
        cfg = resolve_model_config("bin_eval")
        self.assertEqual(cfg["options"], {"reasoning": {"effort": "low"}})

    def test_execute_has_no_max_tokens(self):
        """Non-structured-output nodes (execute) do not set max_tokens."""
        cfg = resolve_model_config("execute")
        self.assertIsNone(cfg["max_tokens"])

    def test_default_routing_covers_all_nodes(self):
        """The DEFAULT_ROUTING fallback map covers every known node name."""
        for node in ["plan", "test_writer", "execute"]:
            self.assertIn(node, DEFAULT_ROUTING, f"{node} missing from DEFAULT_ROUTING")
            self.assertGreater(len(DEFAULT_ROUTING[node]), 0)


class TestResolutionSourceTierLogging(unittest.TestCase):
    """Tests for the resolution-source log line added by issue #126.

    Every precedence path must report the tier that actually supplied each
    value: the winning env var name (Model Config Override), "factory.json",
    or the hardcoded default. Driven exclusively through public surfaces:
    the resolution chain is redirected by patching the public
    FACTORY_JSON_PATH constant at a real temp file — never by mocking
    private helpers.

    Each test resolves a synthetic probe node name that no other test (or
    production code path) ever resolves, so the process-global log-once
    registry has necessarily not seen it yet: first-resolution INFO semantics
    are asserted through observable behavior alone, with no direct
    manipulation of private module state. Assertions check values and tier
    labels co-occurring in a record, not exact log-line wording, so
    alternative valid phrasings stay green.
    """

    _ENV_VARS = [
        "AGENT_MODEL",
        "OPENROUTER_API_KEY",
        "LOG_PROBE_OVERRIDE_MODEL",
        "LOG_PROBE_SECRET_MODEL",
        "LOG_PROBE_EMPTY_MODEL",
        "LOG_PROBE_EQUAL_MODEL",
        "AGENT_RECURSION_LIMIT",
        "LOG_PROBE_MALFORMED_RECURSION_LIMIT",
        "LOG_PROBE_BUDGET_RECURSION_LIMIT",
        "AGENT_LOOP_WARN_THRESHOLD",
        "AGENT_LOOP_HARD_LIMIT",
    ]

    def setUp(self):
        self.original_env = {}
        for var in self._ENV_VARS:
            self.original_env[var] = os.environ.get(var)
            os.environ.pop(var, None)
        # Redirect the public factory path constant at a per-test temp file;
        # resolved at call time inside _load_factory_config.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.factory_path = Path(tmp.name) / "factory.json"
        patcher = unittest.mock.patch(
            "orchestrator.config.FACTORY_JSON_PATH", self.factory_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for var, val in self.original_env.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def _write_factory(self, content: dict | str) -> None:
        """Write content to the patched factory path (dict as JSON, str raw)."""
        if isinstance(content, dict):
            self.factory_path.write_text(json.dumps(content), encoding="utf-8")
        else:
            self.factory_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _info_messages(cm) -> list[str]:
        return [r.getMessage() for r in cm.records if r.levelno == logging.INFO]

    @staticmethod
    def _msg_with(messages: list[str], *needles: str) -> str | None:
        """First message containing every needle, else None.

        Verifies that reported values and tier labels co-occur in one record
        without pinning the surrounding log-line wording.
        """
        for message in messages:
            if all(needle in message for needle in needles):
                return message
        return None

    def test_node_env_override_logs_node_var_tier(self):
        """An active {NODE}_MODEL override logs the Model Config Override tier."""
        node = "log_probe_override"
        self._write_factory({node: {"model": "moonshotai/kimi-k2.7-code"}})
        os.environ["LOG_PROBE_OVERRIDE_MODEL"] = "z-ai/glm-5.2"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["model"], "z-ai/glm-5.2")
        override_msg = self._msg_with(
            self._info_messages(cm),
            "Model Config Override",
            "z-ai/glm-5.2",
            "LOG_PROBE_OVERRIDE_MODEL",
        )
        self.assertIsNotNone(override_msg)

    def test_no_secret_material_in_log_line(self):
        """The resolution line never leaks unrelated secret env values."""
        node = "log_probe_secret"
        self._write_factory({})
        os.environ["LOG_PROBE_SECRET_MODEL"] = "z-ai/glm-5.2"
        os.environ["OPENROUTER_API_KEY"] = "sk-supersecret-value"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            resolve_model_config(node)
        joined = "\n".join(r.getMessage() for r in cm.records)
        self.assertNotIn("sk-supersecret-value", joined)
        self.assertNotIn("OPENROUTER_API_KEY", joined)

    def test_agent_env_override_logs_agent_tier(self):
        """AGENT_MODEL winning the precedence reports the AGENT_MODEL tier."""
        node = "log_probe_agent"
        self._write_factory({})
        os.environ["AGENT_MODEL"] = "qwen/qwen4-max"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["model"], "qwen/qwen4-max")
        override_msg = self._msg_with(
            self._info_messages(cm),
            "Model Config Override",
            "AGENT_MODEL",
            "qwen/qwen4-max",
        )
        self.assertIsNotNone(override_msg)

    def test_empty_node_var_falls_to_agent_tier(self):
        """An empty-string node var does not win; AGENT_MODEL tier is reported."""
        node = "log_probe_empty"
        self._write_factory({})
        os.environ["LOG_PROBE_EMPTY_MODEL"] = ""
        os.environ["AGENT_MODEL"] = "qwen/qwen4-max"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["model"], "qwen/qwen4-max")
        info = self._info_messages(cm)
        self.assertIsNotNone(self._msg_with(info, "AGENT_MODEL", "qwen/qwen4-max"))
        self.assertIsNone(self._msg_with(info, "LOG_PROBE_EMPTY_MODEL"))

    def test_factory_resolution_logs_factory_tier(self):
        """Factory resolution reports the factory.json tier without an override."""
        node = "log_probe_factory"
        self._write_factory(
            {node: {"model": "moonshotai/kimi-k2.7-code", "routing": ["Together"]}}
        )
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["model"], "moonshotai/kimi-k2.7-code")
        info = "\n".join(self._info_messages(cm))
        self.assertIsNotNone(
            self._msg_with(
                self._info_messages(cm), "moonshotai/kimi-k2.7-code", "factory.json"
            )
        )
        self.assertNotIn("Override", info)

    def test_missing_node_logs_default_model_tier(self):
        """A node absent from the factory reports the DEFAULT_MODEL tier."""
        node = "log_probe_absent"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["model"], DEFAULT_MODEL)
        self.assertIsNotNone(
            self._msg_with(self._info_messages(cm), DEFAULT_MODEL, "DEFAULT_MODEL")
        )

    def test_malformed_env_reports_tier_actually_used(self):
        """A malformed env value is ignored; the reported tier is the next one."""
        node = "log_probe_malformed"
        self._write_factory(
            {node: {"model": "m", "routing": ["X"], "recursion_limit": 99}}
        )
        os.environ["LOG_PROBE_MALFORMED_RECURSION_LIMIT"] = "not-an-int"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["recursion_limit"], 99)
        info = self._info_messages(cm)
        self.assertIsNotNone(self._msg_with(info, "99", "factory.json"))
        self.assertIsNone(self._msg_with(info, "LOG_PROBE_MALFORMED_RECURSION_LIMIT"))

    def test_malformed_factory_int_reports_default_tier(self):
        """A malformed factory integer falls through; the default tier is reported."""
        node = "log_probe_badint"
        self._write_factory(
            {
                node: {
                    "model": "m",
                    "recursion_limit": "not-an-int",
                    "loop_warn_threshold": "also-bad",
                }
            }
        )
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["recursion_limit"], DEFAULT_RECURSION_LIMIT)
        self.assertEqual(cfg["loop_warn_threshold"], DEFAULT_LOOP_WARN_THRESHOLD)
        info = self._info_messages(cm)
        self.assertIsNotNone(
            self._msg_with(
                info,
                "loop_warn_threshold",
                str(DEFAULT_LOOP_WARN_THRESHOLD),
            )
        )
        self.assertIsNotNone(
            self._msg_with(info, str(DEFAULT_RECURSION_LIMIT), "default")
        )

    def test_recursion_limit_env_override_reported(self):
        """An env-overridden Recursion Budget names its winning env var."""
        node = "log_probe_budget"
        self._write_factory({})
        os.environ["LOG_PROBE_BUDGET_RECURSION_LIMIT"] = "77"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["recursion_limit"], 77)
        self.assertIsNotNone(
            self._msg_with(
                self._info_messages(cm), "77", "LOG_PROBE_BUDGET_RECURSION_LIMIT"
            )
        )

    def test_agent_loop_threshold_override_reported(self):
        """An env-overridden loop threshold names AGENT_LOOP_HARD_LIMIT."""
        node = "log_probe_loop"
        self._write_factory({})
        os.environ["AGENT_LOOP_HARD_LIMIT"] = "9"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        self.assertEqual(cfg["loop_hard_limit"], 9)
        self.assertIsNotNone(
            self._msg_with(
                self._info_messages(cm),
                "loop_hard_limit",
                "9",
                "AGENT_LOOP_HARD_LIMIT",
            )
        )

    def test_defaults_reported_for_loop_thresholds(self):
        """Un-overridden loop thresholds report their default provenance."""
        node = "log_probe_defaults"
        self._write_factory({node: {"model": "m"}})
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            cfg = resolve_model_config(node)
        info = self._info_messages(cm)
        self.assertIsNotNone(
            self._msg_with(
                info, "loop_warn_threshold", str(DEFAULT_LOOP_WARN_THRESHOLD)
            )
        )
        self.assertIsNotNone(
            self._msg_with(
                info,
                "loop_window_size",
                str(DEFAULT_LOOP_WINDOW_SIZE),
                "default",
            )
        )
        self.assertEqual(cfg["loop_warn_threshold"], DEFAULT_LOOP_WARN_THRESHOLD)

    def test_override_equal_to_factory_still_reports_env_tier(self):
        """An env override naming the factory's model still reports the override.

        The override is active and would mask subsequent factory.json edits,
        so the log must attribute the model to the env tier, not the factory.
        """
        node = "log_probe_equal"
        self._write_factory(
            {node: {"model": "moonshotai/kimi-k2.7-code", "routing": ["X"]}}
        )
        os.environ["LOG_PROBE_EQUAL_MODEL"] = "moonshotai/kimi-k2.7-code"
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            resolve_model_config(node)
        info = self._info_messages(cm)
        self.assertIsNotNone(
            self._msg_with(
                info,
                "Model Config Override",
                "LOG_PROBE_EQUAL_MODEL",
                "moonshotai/kimi-k2.7-code",
            )
        )
        self.assertIsNone(self._msg_with(info, "factory.json"))

    def test_repeat_resolution_downgraded_to_debug(self):
        """The same node resolves ~10x per cycle; only the first logs INFO."""
        node = "log_probe_repeat"
        self._write_factory({node: {"model": "m"}})
        with self.assertLogs("orchestrator.config", level="DEBUG") as cm:
            resolve_model_config(node)
            resolve_model_config(node)
        self.assertEqual(len(self._info_messages(cm)), 1)
        # The factory entry above avoids the absent-node fallback notice, so
        # exactly one DEBUG record may carry the probe: the repeat's
        # resolution-source line.
        source_debug = [
            r
            for r in cm.records
            if r.levelno == logging.DEBUG and node in r.getMessage()
        ]
        self.assertEqual(len(source_debug), 1)

    def test_distinct_nodes_each_get_one_info_line(self):
        """The log-once registry is per-node, not per-process-global."""
        node_a = "log_probe_multi_a"
        node_b = "log_probe_multi_b"
        self._write_factory({})
        with self.assertLogs("orchestrator.config", level="INFO") as cm:
            resolve_model_config(node_a)
            resolve_model_config(node_b)
        nodes = self._info_messages(cm)
        self.assertEqual(len(nodes), 2)
        self.assertIn(node_a, nodes[0])
        self.assertIn(node_b, nodes[1])


if __name__ == "__main__":
    unittest.main()
