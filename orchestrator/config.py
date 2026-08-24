import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from orchestrator.openrouter_chat import OpenRouterAnnotationChatOpenAI

logger = logging.getLogger("orchestrator.config")

# Hardcoded fallback used when factory.json is missing/malformed or a node is
# absent from it. Deliberate deviation from issue #36's named default
# (google/gemini-2.5-pro): deepseek-v4-flash is a verified OpenRouter model
# that keeps the degraded path cost-safe. See ADR-0018.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

# Provider routing fallbacks — mirror of config/factory.json.
# Ensures CI always uses known-good providers even without factory.json,
# preventing OpenRouter from routing to providers with active guardrails
# that return empty responses (e.g. kimi-k2.7-code via non-DeepInfra
# providers). Used only in the degraded path (missing/malformed factory or
# absent node); factory.json routing takes precedence when present.
DEFAULT_ROUTING: dict[str, list[str]] = {
    "plan": ["DeepInfra", "SiliconFlow", "Novita", "Parasail", "DeepSeek"],
    "test_writer": ["Together", "SiliconFlow", "MoonshotAI", "Inceptron"],
    "execute": ["Together", "SiliconFlow", "MoonshotAI", "Inceptron"],
    "bin_eval": ["DeepInfra", "SiliconFlow", "Novita", "Parasail", "DeepSeek"],
}

# LLM call resilience: max retries on transient API errors and a generous
# timeout to allow long reasoning generations without indefinite hangs.
LLM_MAX_RETRIES = 3
LLM_TIMEOUT = 600.0

# Default Worker Recursion Budget (ADR-0045). LangGraph's own default of 25 is a
# crash guard for infinite loops, not a real task budget — "complex graphs may
# hit the default limit naturally" (official LangGraph docs). In a create_agent
# ReAct loop each think→tool round consumes 2 supersteps (agent node + tools
# node), so 50 ≈ 25 tool iterations: enough headroom for non-trivial tasks
# (e.g. scaffolding) while still bounding runaway loops. Overridable per node
# via factory.json ("recursion_limit") or env ({NODE}_RECURSION_LIMIT /
# AGENT_RECURSION_LIMIT), mirroring the Model Config override precedence.
DEFAULT_RECURSION_LIMIT = 50

# Default Tool Loop Detection thresholds (issue #121 / ADR-0046). Loop
# detection is an earlier, cheaper layer than the Recursion Budget crash
# guard: it breaks the ReAct loop when the Worker repeats an identical
# (name + arguments) tool call — the failure mode where a model (notably
# non-OpenAI ones) re-issues e.g. `pip install ...` until GraphRecursionError,
# burning the whole budget without progress. Like recursion_limit, these are
# co-resolved per node alongside the Model Config (no magic constants in the
# worker). Keying is on name + FULL arguments, so legitimately-varied calls
# (e.g. paginated reads with a different offset) are not flagged; warn >= 2
# avoids stopping a transient retry that succeeds on the second attempt.
DEFAULT_LOOP_WARN_THRESHOLD = 3
DEFAULT_LOOP_HARD_LIMIT = 5
# Window = 2 * hard_limit by default so a 2-cycle oscillation (A->B->A->B...)
# can accumulate to hard_limit within the window. Overridable per node.
DEFAULT_LOOP_WINDOW_SIZE = 10

# Path resolution: config/factory.json relative to the project root
# (the parent of this module's package directory).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
FACTORY_JSON_PATH = _PROJECT_ROOT / "config" / "factory.json"


def _load_factory_config(filepath: Path = FACTORY_JSON_PATH) -> dict[str, Any]:
    """Load and parse config/factory.json. Returns an empty dict on missing or
    malformed files, logging a warning. Never raises — the resolver degrades
    gracefully to DEFAULT_MODEL.

    No caching: the orchestrator is a single long-lived process, but
    resolve_model_config is called only ~10 times per issue cycle. Parsing a
    small JSON file is microseconds; caching would add invalidation
    complexity for no measurable benefit.
    """
    if not filepath.exists():
        logger.warning(
            "Factory configuration not found at %s; falling back to default "
            "model %s for all nodes.",
            filepath,
            DEFAULT_MODEL,
        )
        return {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "Factory configuration at %s is malformed (%s); falling back to "
            "default model %s for all nodes.",
            filepath,
            e,
            DEFAULT_MODEL,
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "Factory configuration at %s is not a JSON object; falling back "
            "to default model %s for all nodes.",
            filepath,
            DEFAULT_MODEL,
        )
        return {}

    return data


def _resolve_recursion_limit(
    node_name: str, factory_cfg: dict | None
) -> tuple[int, str]:
    """Resolve the per-node Worker Recursion Budget (ADR-0045).

    Precedence (mirrors the model resolution precedence so the budget is
    co-located with the rest of the per-node config, not a magic constant):
      1. Node-specific env var: f"{node_name.upper()}_RECURSION_LIMIT"
         (e.g. EXECUTE_RECURSION_LIMIT)
      2. General env var: AGENT_RECURSION_LIMIT
      3. factory.json "recursion_limit" for the node
      4. Hardcoded DEFAULT_RECURSION_LIMIT constant

    Distinct budgets per node are supported (e.g. execute vs test_writer) by
    setting a per-node env var or a per-node factory.json entry. A malformed
    env value is logged and ignored, degrading to the next precedence tier
    rather than crashing the run.

    Returns:
        (value, source_tier) where source_tier names the precedence tier that
        actually supplied the value: the winning env variable name,
        "factory.json", or "default". Reported by the resolution-source log
        line so silent drift between tiers is diagnosable (issue #126).
    """
    node_var = f"{node_name.upper()}_RECURSION_LIMIT"
    node_val = os.getenv(node_var)
    agent_val = os.getenv("AGENT_RECURSION_LIMIT")
    env_val = node_val or agent_val
    if env_val:
        try:
            return int(env_val), node_var if node_val else "AGENT_RECURSION_LIMIT"
        except (TypeError, ValueError):
            logger.warning(
                "Invalid recursion_limit env value %r for node '%s'; ignoring "
                "and falling back to factory/default.",
                env_val,
                node_name,
            )
    if factory_cfg and factory_cfg.get("recursion_limit") is not None:
        try:
            return int(factory_cfg["recursion_limit"]), "factory.json"
        except (TypeError, ValueError):
            logger.warning(
                "Invalid recursion_limit in factory.json for node '%s' (%r); "
                "falling back to default %d.",
                node_name,
                factory_cfg["recursion_limit"],
                DEFAULT_RECURSION_LIMIT,
            )
    return DEFAULT_RECURSION_LIMIT, "default"


def _resolve_int_env(
    node_name: str, factory_cfg: dict | None, factory_key: str, default: int
) -> tuple[int, str]:
    """Resolve a per-node integer config value with env precedence.

    Shared by loop-detection thresholds (ADR-0046), mirroring the precedence
    of ``_resolve_recursion_limit`` (ADR-0045):
      1. Node-specific env: f"{node_name.upper()}_{ENV_KEY}"
      2. General env: AGENT_{ENV_KEY}
      3. factory.json entry (factory_key) for the node
      4. Hardcoded default

    A malformed value (env or factory) is logged and ignored, degrading to the
    next precedence tier rather than crashing the run.

    Returns:
        (value, source_tier) where source_tier names the precedence tier that
        actually supplied the value: the winning env variable name,
        "factory.json", or "default" (issue #126).
    """
    env_key = factory_key.upper()  # e.g. loop_hard_limit -> LOOP_HARD_LIMIT
    node_var = f"{node_name.upper()}_{env_key}"
    agent_var = f"AGENT_{env_key}"
    node_val = os.getenv(node_var)
    agent_val = os.getenv(agent_var)
    env_val = node_val or agent_val
    if env_val:
        try:
            return int(env_val), node_var if node_val else agent_var
        except (TypeError, ValueError):
            logger.warning(
                "Invalid %s env value %r for node '%s'; ignoring and "
                "falling back to factory/default.",
                env_key,
                env_val,
                node_name,
            )
    if factory_cfg and factory_cfg.get(factory_key) is not None:
        try:
            return int(factory_cfg[factory_key]), "factory.json"
        except (TypeError, ValueError):
            logger.warning(
                "Invalid %s in factory.json for node '%s' (%r); falling "
                "back to default %d.",
                factory_key,
                node_name,
                factory_cfg[factory_key],
                default,
            )
    return default, "default"


def _resolve_loop_config(
    node_name: str, factory_cfg: dict | None
) -> tuple[dict[str, int], dict[str, str]]:
    """Resolve the per-node Tool Loop Detection config (issue #121 / ADR-0046).

    Returns a ``(values, sources)`` pair of dicts keyed identically by
    ``{"loop_warn_threshold", "loop_hard_limit", "loop_window_size"}``:
    ``values`` holds the resolved integers co-located with the rest of the
    per-node Model Config; ``sources`` holds each field's Resolution Source
    Tier for the resolution-source log line (issue #126). Each field follows
    the env -> factory.json -> default precedence (see ``_resolve_int_env``).
    Distinct values per node (execute vs test_writer) are supported via
    per-node env vars or factory.json entries. A ``loop_hard_limit <= 0``
    disables loop detection for that node.
    """
    keys = ("loop_warn_threshold", "loop_hard_limit", "loop_window_size")
    defaults = (
        DEFAULT_LOOP_WARN_THRESHOLD,
        DEFAULT_LOOP_HARD_LIMIT,
        DEFAULT_LOOP_WINDOW_SIZE,
    )
    resolved = [
        _resolve_int_env(node_name, factory_cfg, key, default)
        for key, default in zip(keys, defaults, strict=True)
    ]
    values = {key: value for key, (value, _) in zip(keys, resolved, strict=True)}
    sources = {key: source for key, (_, source) in zip(keys, resolved, strict=True)}
    return values, sources


# Nodes whose resolution-source summary has already been logged at INFO in
# this process (issue #126). resolve_model_config is called ~10x per issue
# cycle by design (no caching), so the first resolution per node logs at INFO
# and every repeat degrades to DEBUG to avoid log spam.
_SOURCE_TIER_LOGGED: set[str] = set()


def _log_resolution_source(
    node_name: str,
    cfg: dict[str, Any],
    model_tier: str,
    recursion_tier: str,
    loop_tiers: dict[str, str],
) -> None:
    """Emit the one-line Model Config resolution-source summary (issue #126).

    Makes the 4-tier precedence observable: on 2026-08-14 the workers ran a
    different model than config/factory.json named because an AGENT_MODEL
    export outside .env silently won — invisible in the logs. The line names
    the resolved values plus the tier each came from: the winning env var
    name (a "Model Config Override" per the glossary), "factory.json", or
    the hardcoded default.

    Secrets are never logged: only model ids, integer thresholds, env
    variable NAMES, and routing-free tier labels — no API keys and no env
    values beyond the resolved config itself. First resolution of a node per
    process logs at INFO; repeats log at DEBUG.
    """
    override_active = model_tier not in ("factory.json", "DEFAULT_MODEL")
    prefix = (
        "Model Config Override active" if override_active else "Resolved Model Config"
    )
    message = (
        f"{prefix} (node={node_name}): model={cfg['model']} "
        f"(source={model_tier}); recursion_limit={cfg['recursion_limit']} "
        f"(source={recursion_tier}); "
        f"loop_warn_threshold={cfg['loop_warn_threshold']} "
        f"(source={loop_tiers['loop_warn_threshold']}); "
        f"loop_hard_limit={cfg['loop_hard_limit']} "
        f"(source={loop_tiers['loop_hard_limit']}); "
        f"loop_window_size={cfg['loop_window_size']} "
        f"(source={loop_tiers['loop_window_size']})"
    )
    if node_name in _SOURCE_TIER_LOGGED:
        logger.debug(message)
    else:
        logger.info(message)
        _SOURCE_TIER_LOGGED.add(node_name)


def resolve_model_config(node_name: str) -> dict[str, Any]:
    """Resolve the Model Config for a given orchestrator node.

    Precedence (highest to lowest):
      1. Node-specific env var: f"{node_name.upper()}_MODEL" (e.g. PLAN_MODEL)
      2. General env var: AGENT_MODEL
      3. factory.json entry for the node
      4. Hardcoded DEFAULT_MODEL constant

    When an env override is active, provider routing is disabled (set to
    None) since the override model may not be registered in the factory's
    routing list. Temperature defaults to 0.0 and options to None unless the
    factory entry for the same model provides them.

    Each resolution also reports its source tiers via a single log line
    (INFO on this node's first resolution per process, DEBUG afterwards):
    which precedence tier supplied the model, the Recursion Budget, and the
    loop-detection thresholds — making silent Model Config Override drift a
    30-second diagnosis instead of shell-level archaeology (issue #126).

    Returns:
        {"model": str, "routing": List[str] | None,
         "temperature": float, "options": Dict[str, Any] | None,
         "max_tokens": int | None, "fallback_model": str | None,
         "recursion_limit": int,
         "loop_warn_threshold": int, "loop_hard_limit": int,
         "loop_window_size": int}
    """
    factory = _load_factory_config()
    factory_cfg = factory.get(node_name) if isinstance(factory, dict) else None

    if factory_cfg is not None and not isinstance(factory_cfg, dict):
        logger.debug(
            "Factory entry for node '%s' is not an object; treating as absent.",
            node_name,
        )
        factory_cfg = None

    # Tool Loop Detection config (issue #121 / ADR-0046): co-resolved per node
    # alongside the Recursion Budget, mirroring its precedence. Computed once
    # and spread into every return path so loop detection is configured on all
    # resolution tiers (env-override, factory, hardcoded fallback).
    loop_cfg, loop_tiers = _resolve_loop_config(node_name, factory_cfg)
    recursion_limit, recursion_tier = _resolve_recursion_limit(node_name, factory_cfg)

    # 1 & 2. Environment overrides
    node_env_var = f"{node_name.upper()}_MODEL"
    node_model = os.getenv(node_env_var)
    overridden_model = node_model or os.getenv("AGENT_MODEL") or None

    if overridden_model:
        model_tier = node_env_var if node_model else "AGENT_MODEL"
        # Env override active => disable specific provider routing.
        # Inherit temperature/options from factory only if the factory entry
        # names the same model; otherwise use safe defaults.
        if factory_cfg and factory_cfg.get("model") == overridden_model:
            temperature = factory_cfg.get("temperature", 0.0)
            options = factory_cfg.get("options")
            max_tokens = factory_cfg.get("max_tokens")
        else:
            temperature = 0.0
            options = None
            max_tokens = None
        cfg: dict[str, Any] = {
            "model": overridden_model,
            "routing": None,
            "temperature": temperature,
            "options": options,
            "max_tokens": max_tokens,
            "fallback_model": factory_cfg.get("fallback_model")
            if factory_cfg
            else None,
            "recursion_limit": recursion_limit,
            **loop_cfg,
        }
    elif factory_cfg:
        # 3. Factory configuration
        model_tier = "factory.json"
        cfg = {
            "model": factory_cfg["model"],
            "routing": factory_cfg.get("routing"),
            "temperature": factory_cfg.get("temperature", 0.0),
            "options": factory_cfg.get("options"),
            "max_tokens": factory_cfg.get("max_tokens"),
            "fallback_model": factory_cfg.get("fallback_model"),
            "recursion_limit": recursion_limit,
            **loop_cfg,
        }
    else:
        # 4. Hardcoded fallback (factory missing/malformed or node absent)
        logger.debug(
            "Node '%s' not found in factory configuration; falling back to %s.",
            node_name,
            DEFAULT_MODEL,
        )
        model_tier = "DEFAULT_MODEL"
        cfg = {
            "model": DEFAULT_MODEL,
            "routing": DEFAULT_ROUTING.get(node_name),
            "temperature": 0.0,
            "options": None,
            "max_tokens": None,
            "fallback_model": None,
            "recursion_limit": recursion_limit,
            **loop_cfg,
        }

    _log_resolution_source(node_name, cfg, model_tier, recursion_tier, loop_tiers)
    return cfg


def get_chat_model_from_config(cfg: dict[str, Any]) -> ChatOpenAI:
    """Construct a ChatOpenAI client from a resolved Model Config dict.

    Honors the routing and options fields: the routing list is passed to
    OpenRouter via the extra_body 'provider' parameter; options (e.g.
    thinking-effort) are merged into extra_body and passed through verbatim.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY environment variable is not set.")

    model_name = cfg["model"]
    routing = cfg.get("routing")
    temperature = cfg.get("temperature", 0.0)
    options = cfg.get("options")
    max_tokens = cfg.get("max_tokens")

    extra_body: dict[str, Any] = {}

    if routing:
        extra_body["provider"] = {
            "order": [r.lower() for r in routing],
            "allow_fallbacks": False,
        }
    if options:
        extra_body.update(options)

    return OpenRouterAnnotationChatOpenAI(
        model=model_name,
        api_key=SecretStr(api_key),
        base_url="https://openrouter.ai/api/v1",
        temperature=temperature,
        extra_body=extra_body or None,
        max_retries=LLM_MAX_RETRIES,
        request_timeout=LLM_TIMEOUT,
        use_responses_api=False,
        **({"max_tokens": max_tokens} if max_tokens is not None else {}),
    )
