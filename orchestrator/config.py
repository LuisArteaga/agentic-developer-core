import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

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

    Returns:
        {"model": str, "routing": List[str] | None,
         "temperature": float, "options": Dict[str, Any] | None,
         "fallback_model": str | None}
    """
    factory = _load_factory_config()
    factory_cfg = factory.get(node_name) if isinstance(factory, dict) else None

    if factory_cfg is not None and not isinstance(factory_cfg, dict):
        logger.debug(
            "Factory entry for node '%s' is not an object; treating as absent.",
            node_name,
        )
        factory_cfg = None

    # 1 & 2. Environment overrides
    node_env_var = f"{node_name.upper()}_MODEL"
    overridden_model = os.getenv(node_env_var) or os.getenv("AGENT_MODEL") or None

    if overridden_model:
        # Env override active => disable specific provider routing.
        # Inherit temperature/options from factory only if the factory entry
        # names the same model; otherwise use safe defaults.
        if factory_cfg and factory_cfg.get("model") == overridden_model:
            temperature = factory_cfg.get("temperature", 0.0)
            options = factory_cfg.get("options")
        else:
            temperature = 0.0
            options = None
        return {
            "model": overridden_model,
            "routing": None,
            "temperature": temperature,
            "options": options,
            "fallback_model": factory_cfg.get("fallback_model")
            if factory_cfg
            else None,
        }

    # 3. Factory configuration
    if factory_cfg:
        return {
            "model": factory_cfg["model"],
            "routing": factory_cfg.get("routing"),
            "temperature": factory_cfg.get("temperature", 0.0),
            "options": factory_cfg.get("options"),
            "fallback_model": factory_cfg.get("fallback_model"),
        }

    # 4. Hardcoded fallback (factory missing/malformed or node absent)
    logger.debug(
        "Node '%s' not found in factory configuration; falling back to %s.",
        node_name,
        DEFAULT_MODEL,
    )
    return {
        "model": DEFAULT_MODEL,
        "routing": DEFAULT_ROUTING.get(node_name),
        "temperature": 0.0,
        "options": None,
        "fallback_model": None,
    }


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

    extra_body: dict[str, Any] = {}

    if routing:
        extra_body["provider"] = {
            "order": [r.lower() for r in routing],
            "allow_fallbacks": False,
        }
    if options:
        extra_body.update(options)

    return ChatOpenAI(
        model=model_name,
        api_key=SecretStr(api_key),
        base_url="https://openrouter.ai/api/v1",
        temperature=temperature,
        extra_body=extra_body or None,
        max_retries=LLM_MAX_RETRIES,
        request_timeout=LLM_TIMEOUT,
        use_responses_api=False,
    )
