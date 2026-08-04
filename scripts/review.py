import datetime
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

# Add project root and scripts dir to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

from telemetry import (  # noqa: E402
    INPUT_VALUE,
    LLM_MODEL_NAME,
    OPENINFERENCE_SPAN_KIND,
    OUTPUT_VALUE,
    TOOL_NAME,
    TOOL_PARAMETERS,
    get_tracer,
    init_telemetry,
    trace,
)

from orchestrator.config import resolve_model_config  # noqa: E402

# Setup logger paths
log_file_path = None
agent_log_path = os.getenv("AGENT_LOG_PATH")
if agent_log_path:
    log_dir = os.path.dirname(agent_log_path)
else:
    agent_mode = os.getenv("AGENT_MODE", "ci")
    if agent_mode == "ci":
        workspace = os.getenv("GITHUB_WORKSPACE", ".")
        log_dir = os.path.join(workspace, "agent_logs")
    else:
        log_dir = None


def is_dir_writeable(path):
    try:
        os.makedirs(path, exist_ok=True)
        # Test writeability
        test_file = os.path.join(path, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return True
    except Exception:
        return False


if log_dir:
    if not is_dir_writeable(log_dir):
        sys.stdout.write(
            f"[WARN] Log directory {log_dir} is not writeable (Permission Denied). Falling back to container /tmp/agent_logs\n"
        )
        log_dir = "/tmp/agent_logs"
        if not is_dir_writeable(log_dir):
            log_dir = None

    if log_dir:
        log_file_path = os.path.join(log_dir, "review.log")


def log(message):
    """Logs a message with timestamp to stdout and CI log file if configured."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted)
    if log_file_path:
        try:
            with open(log_file_path, "a") as f:
                f.write(formatted + "\n")
        except Exception:
            pass


# Prompt definitions for LLM-as-a-Judge evaluations
SYSTEM_PROMPT_SYNTAX_LINT = (
    "You are a code reviewer specialized in syntax validation, JSON schemas, and naming conventions.\n"
    "Review the PR diff against these specific criteria:\n"
    "=== 1. CRITERIA DEFINITION ===\n"
    "- Q1 (Syntax Validation): Check if the modified code is free of syntax errors, obvious compilation issues, or typos. (Note: Due to system-level egress sanitization, the '@' symbol used for decorators, e.g. @pytest.fixture or @functools.lru_cache, might be received as '[EMAIL]'. Do NOT count '[EMAIL]' as a syntax error or typo; treat it as a valid '@' decorator symbol).\n"
    "- Q2 (JSON Schema Verification): Check if any modified JSON files adhere to standard or expected JSON formats and schemas.\n"
    "- Q3 (Naming Conventions): Check if class names, functions, and variables follow sensible naming conventions (functions and variables in snake_case, classes in PascalCase).\n\n"
    "=== 2. ARGUMENTATION STRUCTURE ===\n"
    "Output your thought process inside <reasoning>...</reasoning> tags.\n"
    "Output any violations inside <findings>...</findings> tags.\n\n"
    "=== 3. SCORING RULE ===\n"
    "- PASS: If there are no violations. Output an empty findings block: <findings></findings>.\n"
    "- FAIL: If one or more criteria fail. Report each violation as a JSON object on a single line inside the findings block: "
    '{"severity": "error", "message": "[QX] Details of the failure"}\n'
    'Example: If Q3 fails: {"severity": "error", "message": "[Q3] Class FooBar does not use PascalCase"}\n\n'
    "=== 4. EDGE-CASE HANDLING ===\n"
    "- If the diff is empty, return PASS with empty findings.\n\n"
    "=== OUTPUT FORMAT ===\n"
    "First, output your reasoning block:\n"
    "<reasoning>\n"
    "[Your reasoning/thinking about the syntax and naming aspects]\n"
    "</reasoning>\n\n"
    "Second, output your findings block:\n"
    "<findings>\n"
    "[Line-delimited JSON objects if FAIL, otherwise empty]\n"
    "</findings>"
)

SYSTEM_PROMPT_TEST_COVERAGE = (
    "You are a code reviewer specialized in test validation and coverage.\n"
    "Review the PR diff against these specific criteria:\n"
    "=== 1. CRITERIA DEFINITION ===\n"
    "- Q1 (Test Presence): Check if any modified logic or new code is accompanied by new tests or updates to existing tests in the test folders.\n"
    "- Q2 (Test Quality/Assertions): Check if the tests contain meaningful assertions validating the actual behavior/logic changes, rather than trivial or empty test cases.\n\n"
    "=== 2. ARGUMENTATION STRUCTURE ===\n"
    "Output your thought process inside <reasoning>...</reasoning> tags.\n"
    "Output any violations inside <findings>...</findings> tags.\n\n"
    "=== 3. SCORING RULE ===\n"
    "- PASS: If there are no violations. Output an empty findings block: <findings></findings>.\n"
    "- FAIL: If one or more criteria fail. Report each violation as a JSON object on a single line inside the findings block: "
    '{"severity": "error", "message": "[QX] Details of the failure"}\n'
    'Example: If Q1 fails: {"severity": "error", "message": "[Q1] No tests added for new function compute_hash"}\n\n'
    "=== 4. EDGE-CASE HANDLING ===\n"
    "- If the diff is empty, return PASS with empty findings.\n\n"
    "=== OUTPUT FORMAT ===\n"
    "First, output your reasoning block:\n"
    "<reasoning>\n"
    "[Your reasoning/thinking about the tests]\n"
    "</reasoning>\n\n"
    "Second, output your findings block:\n"
    "<findings>\n"
    "[Line-delimited JSON objects if FAIL, otherwise empty]\n"
    "</findings>"
)

SYSTEM_PROMPT_ARCH = (
    "You are a code reviewer specialized in architecture compliance. Review the PR diff for compliance with repository architecture, conventions, and design decisions.\n\n"
    "=== 1. CRITERIA DEFINITION ===\n"
    "Check the diff for compliance against the documented architecture rules, ADRs, context conventions, and the following general rules:\n"
    "- Adherence to Conventional Commit format in modified titles/messages.\n"
    "- Radical simplicity / Lazy Coding: Avoid unnecessary abstractions, boilerplate, redundant interfaces, or scaffolding for future use.\n"
    "- Prefer standard library (stdlib) functions and native features over adding new dependencies.\n"
    "- Deletion of unused code over keeping dead code.\n\n"
    "=== 2. ARGUMENTATION STRUCTURE ===\n"
    "For each compliance deviation, explain why the design violates the simple/lazy guidelines or specific ADR rules.\n"
    "Structure your response by outputting your thought process inside <reasoning>...</reasoning> tags.\n"
    "Then, output any compliance findings inside <findings>...</findings> tags.\n\n"
    "=== 3. SCORING RULE ===\n"
    "- PASS: If the code complies with all architectural conventions. Output an empty findings block: <findings></findings>.\n"
    '- FAIL: If any compliance deviation is found. Report each as a JSON object on a single line inside the findings block: {"severity": "bug", "message": "..."}.\n'
    "- NEEDS REVIEW: If key context documents are missing and you cannot confirm compliance, log reasoning and output empty findings.\n\n"
    "=== 4. EDGE-CASE HANDLING ===\n"
    "- If the prompt indicates that context files are missing, evaluate compliance purely against the general simplicity/lazy coding rules and conventional commits.\n"
    "- Do NOT flag intentional scaffolding that is explicitly requested in the issue requirements.\n"
    "- If the diff is empty, return PASS with empty findings.\n\n"
    "=== OUTPUT FORMAT ===\n"
    "First, output your reasoning block:\n"
    "<reasoning>\n"
    "[Your reasoning/thinking about the architectural compliance of the changes]\n"
    "</reasoning>\n\n"
    "Second, output your findings block:\n"
    "<findings>\n"
    "[Line-delimited JSON objects if FAIL, otherwise empty]\n"
    "</findings>"
)

SYSTEM_PROMPT_SECURITY = (
    "You are a code reviewer specialized in security. Review the PR diff for critical security issues.\n\n"
    "=== 1. CRITERIA DEFINITION ===\n"
    "Check the diff for the following critical security vulnerabilities:\n"
    "- Hardcoded credentials, secrets, passwords, or API keys.\n"
    "- Injection vulnerabilities (e.g., shell command execution without escaping, SQL injection).\n"
    "- Insecure authentication/authorization bypasses.\n"
    "- Insecure data storage or transmission of sensitive data.\n\n"
    "=== 2. ARGUMENTATION STRUCTURE ===\n"
    "For each potential issue, explain the exact attack vector and business impact.\n"
    "Structure your response by outputting your thought process inside <reasoning>...</reasoning> tags.\n"
    "Then, output any found vulnerabilities inside <findings>...</findings> tags.\n\n"
    "=== 3. SCORING RULE ===\n"
    "- PASS: If there are no security vulnerabilities. Output an empty findings block: <findings></findings>.\n"
    '- FAIL: If one or more verified security vulnerabilities are found. Report each as a JSON object on a single line inside the findings block: {"severity": "security", "message": "..."}.\n'
    "- NEEDS REVIEW: If there is insufficient context to verify, explain why in reasoning and output an empty findings block.\n\n"
    "=== 4. EDGE-CASE HANDLING ===\n"
    "- Do NOT flag placeholder values in test files, configuration templates, or mock setups as vulnerabilities.\n"
    "- Do NOT flag intentional, safe usages of low-level commands that are thoroughly sanitised.\n"
    "- If the diff is empty, return PASS with empty findings.\n\n"
    "=== OUTPUT FORMAT ===\n"
    "First, output your reasoning block:\n"
    "<reasoning>\n"
    "[Your reasoning/thinking about the security aspects of the code changes]\n"
    "</reasoning>\n\n"
    "Second, output your findings block:\n"
    "<findings>\n"
    "[Line-delimited JSON objects if FAIL, otherwise empty]\n"
    "</findings>"
)

BATCH_BUDGET_CHARS = 200000

EMPTY_CONTENT_INSTRUCTION = (
    "\n\nYour previous response was empty. Please provide a verdict "
    "with <reasoning> and <findings> tags."
)


def run_command(cmd, env=None):
    """Runs a shell command and returns code, stdout, stderr."""
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return res.returncode, res.stdout, res.stderr


def build_openrouter_provider(routing):
    """Build the OpenRouter provider payload from a routing list, unified with orchestrator/config.py."""
    if routing:
        return {"order": [r.lower() for r in routing], "allow_fallbacks": False}
    return None


def build_payload(model, messages, routing, temperature, options):
    """Build the OpenRouter chat completions request payload dict."""
    payload_dict: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature if temperature is not None else 0.0,
    }
    provider = build_openrouter_provider(routing)
    if provider:
        payload_dict["provider"] = provider
    if options:
        payload_dict.update(options)
    return payload_dict


def call_openrouter_api(
    model, messages, api_key, routing=None, temperature=0.0, options=None
):
    """Performs HTTP request to OpenRouter chat completions API."""
    url = "https://openrouter.ai/api/v1/chat/completions"

    payload_dict = build_payload(model, messages, routing, temperature, options)
    payload = json.dumps(payload_dict)
    data = payload.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "agentic-developer-core/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as response:  # nosemgrep  # fmt: skip
        return response.status, response.read().decode("utf-8")


def _call_with_api_retry(model, messages, api_key, routing, temperature, options):
    """Single OpenRouter call with 2-attempt API-error retry and structural
    validation.

    Returns the raw response body string on success.
    Raises Exception on API-level failure after retries.
    Does NOT check for empty content — that is the caller's responsibility.
    """
    last_error = ""
    for attempt in range(2):
        try:
            status, body = call_openrouter_api(
                model,
                messages,
                api_key,
                routing=routing,
                temperature=temperature,
                options=options,
            )
            parsed_body = json.loads(body, strict=False)
            if "error" in parsed_body:
                err = parsed_body["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise Exception(f"OpenRouter API error: {msg}")
            elif "choices" not in parsed_body or not parsed_body["choices"]:
                raise Exception("OpenRouter response missing choices block")
            return body
        except Exception as e:
            last_error = str(e)
            log(f"[WARN] OpenRouter attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                time.sleep(3)
                continue
            raise Exception(
                f"LLM review failed after retries. Last error: {last_error}"
            )


def _is_empty_content(raw_response: str) -> bool:
    """Check if the response content is empty or whitespace-only."""
    data = json.loads(raw_response, strict=False)
    content = data["choices"][0]["message"]["content"]
    return not content or not content.strip()


def _run_layered_retry(
    judge_key, model, messages, fallback_model, api_key, routing, temperature, options
):
    """Execute the layered empty-content retry + model fallback policy (ADR-0021).

    Wraps the single-call transport (``_call_with_api_retry``) with the
    empty-content quality check and the fallback-model progression, kept
    separate from config resolution and telemetry so the policy is testable in
    isolation (issue #51). This function owns only one concern: given a model,
    its messages, and an optional fallback, drive the transport until a
    non-empty response is obtained or the progression is exhausted.

    Retry progression (each attempt already carries its own 2-attempt API-error
    retry inside ``_call_with_api_retry``):
        1. Primary model, original prompt.
        2. Primary model, explicit-instruction nudge - only if (1) is empty.
        3. Fallback model, original prompt, routing=None, options=None,
           temperature=0.0 - only if (2) is empty AND a fallback_model is set.
        4. Give up: return the last (empty) body; ``run_judge``'s caller maps an
           empty body to ``NEEDS REVIEW`` via ``evaluate_response``.

    Args:
        judge_key: judge identifier, used only for log messages.
        model: primary model id.
        messages: ``[{system, ...}, {user, ...}]`` prompt messages. The nudge
            attempt reuses these messages with ``EMPTY_CONTENT_INSTRUCTION``
            appended to the last (user) turn.
        fallback_model: optional fallback model id (may be ``None``).
        api_key, routing, temperature, options: forwarded to the transport.

    Returns:
        ``(response_body, used_fallback, final_model, attempt_count)``.
    """
    response_body = _call_with_api_retry(
        model, messages, api_key, routing, temperature, options
    )
    attempt_count = 1
    used_fallback = False
    final_model = model

    if _is_empty_content(response_body):
        log(
            f"[WARN] Judge {judge_key}: empty content from primary model, "
            f"retrying with explicit instruction"
        )
        # Attempt 2: primary model, explicit-instruction nudge on the last turn.
        nudge_messages = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        nudge_messages[-1]["content"] += EMPTY_CONTENT_INSTRUCTION
        response_body = _call_with_api_retry(
            model, nudge_messages, api_key, routing, temperature, options
        )
        attempt_count = 2

        if _is_empty_content(response_body) and fallback_model:
            log(f"[INFO] Judge {judge_key} fell back to model {fallback_model}")
            # Attempt 3: fallback model, original prompt,
            # routing=None, options=None, temperature=0.0 (ADR-0021).
            response_body = _call_with_api_retry(
                fallback_model, messages, api_key, None, 0.0, None
            )
            attempt_count = 3
            used_fallback = True
            final_model = fallback_model

    return response_body, used_fallback, final_model, attempt_count


def call_llm_for_review(judge_key, system_prompt, diff, api_key):
    """Resolve config for judge_key and run the layered retry/fallback policy
    under an OpenRouter chat completion trace span.

    Three concerns, kept separate (issue #51):

      - **Config resolution** - the single boundary between callers and the
        factory (``resolve_model_config``); config is runtime data, not
        interleaved with the call mechanism.
      - **Retry/fallback policy** - delegated to ``_run_layered_retry``, which
        owns the empty-content check and model fallback progression. Kept
        free of telemetry so it is unit-testable in isolation.
      - **Telemetry** - one ``openrouter_chat_completion`` span with input,
        output, ``used_fallback`` and ``final_model`` attributes (ADR-0021's
        "one span, one return path").

    Returns:
        ``(response_body: str, metadata: dict)`` where metadata is
        ``{"used_fallback": bool, "final_model": str, "attempt_count": int}``.
    """
    cfg = resolve_model_config(judge_key)
    model = cfg["model"]
    routing = cfg["routing"]
    temperature = cfg["temperature"]
    options = cfg["options"]
    fallback_model = cfg.get("fallback_model")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": diff},
    ]

    tracer = get_tracer()
    with tracer.start_as_current_span("openrouter_chat_completion") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        span.set_attribute(LLM_MODEL_NAME, model)
        span.set_attribute(INPUT_VALUE, json.dumps(messages))
        log(f"[INFO] Running judge {judge_key} using model: {model}")

        response_body, used_fallback, final_model, attempt_count = _run_layered_retry(
            judge_key,
            model,
            messages,
            fallback_model,
            api_key,
            routing,
            temperature,
            options,
        )

        span.set_attribute(OUTPUT_VALUE, response_body)
        span.set_attribute("used_fallback", used_fallback)
        span.set_attribute("final_model", final_model)
        return response_body, {
            "used_fallback": used_fallback,
            "final_model": final_model,
            "attempt_count": attempt_count,
        }


def submit_github_review(pr_number, action, body_content):
    """Submits findings using GitHub CLI wrapped in a trace span."""
    tracer = get_tracer()
    with tracer.start_as_current_span("submit_github_review") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "TOOL")
        span.set_attribute(TOOL_NAME, "submit_github_review")
        span.set_attribute(
            TOOL_PARAMETERS,
            json.dumps(
                {"pr_number": pr_number, "action": action, "body_content": body_content}
            ),
        )
        span.set_attribute(
            INPUT_VALUE,
            json.dumps(
                {"pr_number": pr_number, "action": action, "body_content": body_content}
            ),
        )

        # Get PR Author
        pr_author_cmd = [
            "gh",
            "pr",
            "view",
            pr_number,
            "--json",
            "author",
            "--jq",
            ".author.login",
        ]
        ret, stdout, stderr = run_command(pr_author_cmd)
        if ret != 0:
            raise Exception(f"Failed to fetch PR author: {stderr.strip()}")
        pr_author = stdout.strip()

        # Get Current User
        user_cmd = ["gh", "api", "user", "--jq", ".login"]
        ret, stdout, stderr = run_command(user_cmd)
        if ret != 0:
            raise Exception(f"Failed to fetch current user: {stderr.strip()}")
        current_user = stdout.strip()

        # Determine appropriate review action flag
        if current_user == pr_author:
            action_flag = "--comment"
        elif action == "approve":
            action_flag = "--approve"
        elif action == "comment":
            action_flag = "--comment"
        else:
            action_flag = "--request-changes"

        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".md") as temp:
            temp.write(body_content)
            temp_path = temp.name

        try:
            review_cmd = [
                "gh",
                "pr",
                "review",
                pr_number,
                action_flag,
                "--body-file",
                temp_path,
            ]
            ret, stdout, stderr = run_command(review_cmd)

            span.set_attribute(
                OUTPUT_VALUE,
                json.dumps({"exit_code": ret, "stdout": stdout, "stderr": stderr}),
            )

            if ret != 0:
                raise Exception(f"gh pr review failed: {stderr.strip()}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


def parse_xml_tags(text: str, open_tag: str, close_tag: str) -> str:
    """Helper to extract content between open_tag and close_tag."""
    if open_tag not in text:
        return ""
    last_open = text.rfind(open_tag)
    block = text[last_open + len(open_tag) :]
    close_idx = block.find(close_tag)
    if close_idx != -1:
        return block[:close_idx].strip()
    return block.strip()


def evaluate_response(raw_response: str) -> tuple[str, str, list[str]]:
    """Evaluates the LLM response.

    Returns (verdict, reasoning, findings_list)
    where verdict is 'Pass', 'Fail', or 'Needs Review'.
    """
    data = json.loads(raw_response, strict=False)
    content = data["choices"][0]["message"]["content"]

    if not content:
        return "Needs Review", "Empty response from LLM", []

    reasoning = parse_xml_tags(content, "<reasoning>", "</reasoning>")
    findings_block = parse_xml_tags(content, "<findings>", "</findings>")

    # Check for refusal / lack of tags
    if not reasoning and not findings_block:
        return (
            "Needs Review",
            "Response lacks both <reasoning> and <findings> tags. Original output:\n"
            + content,
            [],
        )

    findings_list = []
    verdict = "Pass"

    for line in findings_block.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line, strict=False)
            if not isinstance(f, dict):
                continue
            sev = f.get("severity", "bug").lower()
            msg = f.get("message", "").replace("\n", " ")
            findings_list.append(f"{sev}|{msg}")
            verdict = "Fail"
        except Exception:
            continue

    return verdict, reasoning, findings_list


def load_architecture_context(workspace_dir: str) -> str:
    """Loads docs/context.md and all docs/adr/*.md files relative to workspace_dir."""
    context_lines = []
    docs_dir = os.path.join(workspace_dir, "docs")

    # Try reading context.md
    context_file = os.path.join(docs_dir, "context.md")
    if os.path.isfile(context_file):
        try:
            with open(context_file, "r", encoding="utf-8", errors="replace") as f:
                context_lines.append("--- docs/context.md ---")
                context_lines.append(f.read())
                context_lines.append("")
        except Exception as e:
            sys.stdout.write(f"[WARN] Failed to read {context_file}: {e}\n")
    else:
        sys.stdout.write(
            f"[WARN] Architecture context file {context_file} is missing.\n"
        )

    # Try reading adr/*.md files
    adr_dir = os.path.join(docs_dir, "adr")
    if os.path.isdir(adr_dir):
        try:
            for entry in sorted(os.listdir(adr_dir)):
                if entry.endswith(".md"):
                    entry_path = os.path.join(adr_dir, entry)
                    if os.path.isfile(entry_path):
                        with open(
                            entry_path, "r", encoding="utf-8", errors="replace"
                        ) as f:
                            context_lines.append(f"--- docs/adr/{entry} ---")
                            context_lines.append(f.read())
                            context_lines.append("")
        except Exception as e:
            sys.stdout.write(f"[WARN] Failed to read ADR files from {adr_dir}: {e}\n")
    else:
        sys.stdout.write(
            f"[WARN] Architecture Decision Records folder {adr_dir} is missing.\n"
        )

    return "\n".join(context_lines)


# Maximum characters of CI coverage output fed to the Test Coverage judge.
# Bounded to keep the judge's context manageable (LLM-as-judge best practice:
# bounded, focused context). Tail-bounded (not head) because pytest prints the
# per-file coverage table and the pass/fail summary at the *end* of its
# output — the head is test progress dots, which carry little evaluative
# signal. This is a deliberate, documented deviation from ``clip_chunk``'s
# head-truncation; see ADR-0030.
CI_COVERAGE_OUTPUT_MAX_CHARS = 15000


def _get_ci_coverage_output_budget() -> int:
    """Resolve the CI coverage output character budget, overridable via env."""
    return int(
        os.getenv("CI_COVERAGE_OUTPUT_MAX_CHARS", str(CI_COVERAGE_OUTPUT_MAX_CHARS))
    )


def _tail_clip(text: str, budget: int) -> str:
    """Clip ``text`` to its last ``budget`` characters, prefixing a note.

    Tail-clipping preserves the pytest coverage table and the final test
    summary, which pytest emits at the end of its stream — the most evaluative
    signal for the Test Coverage judge.
    """
    if len(text) <= budget:
        return text
    note = (
        f"[NOTE: CI output truncated to last {budget} chars; head omitted "
        "because the coverage table and test summary are at the end.]\n"
    )
    return note + text[-(budget):]


def load_ci_coverage_output() -> str:
    """Load CI-produced test + coverage output for the Test Coverage judge.

    Resolves the file path from the ``CI_COVERAGE_OUTPUT_PATH`` environment
    variable (set by the CI workflow), reads it, and tail-clips it to the
    configured character budget. Returns an empty string when the env var is
    unset, the file is missing, or the content is empty — callers must treat
    an empty result as "no CI output available" and degrade to diff-only
    evaluation (Language Agnosticism + graceful degradation; ADR-0030).

    The judge never runs language-specific tooling itself; it consumes this
    output produced by the target repository's own test suite.
    """
    path = os.getenv("CI_COVERAGE_OUTPUT_PATH", "")
    if not path:
        log(
            "[INFO] CI_COVERAGE_OUTPUT_PATH not set; test_coverage judge runs diff-only"
        )
        return ""

    if not os.path.isfile(path):
        log(
            f"[WARN] CI coverage output file not found at {path}; test_coverage judge runs diff-only"
        )
        return ""

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        log(
            f"[WARN] Failed to read CI coverage output at {path}: {e}; test_coverage judge runs diff-only"
        )
        return ""

    if not content.strip():
        log(
            f"[WARN] CI coverage output at {path} is empty; test_coverage judge runs diff-only"
        )
        return ""

    budget = _get_ci_coverage_output_budget()
    clipped = _tail_clip(content, budget)
    if len(content) > budget:
        log(
            f"[INFO] CI coverage output clipped from {len(content)} to last "
            f"{budget} chars for test_coverage judge"
        )
    return clipped


def build_test_coverage_ci_augmentation(ci_output: str) -> str:
    """Build the CI-augmentation block appended to the Test Coverage prompt.

    Appends the CI test/coverage output and two additional criteria — Q3
    (Test Execution) and Q4 (Coverage of Changed Code) — to the base Test
    Coverage system prompt. The augmentation is only produced when CI output
    is available; the base prompt's Q1/Q2 remain the diff-only fallback.

    Returns an empty string when ``ci_output`` is empty so callers can simply
    append the result unconditionally.
    """
    if not ci_output or not ci_output.strip():
        return ""

    return (
        "\n\n=== CI VERIFICATION & COVERAGE OUTPUT ===\n"
        "The CI pipeline ran the repository's own test suite with coverage. "
        "The output below contains the test execution summary and a per-file "
        "coverage report (including missing/uncovered line numbers). Use it to "
        "evaluate the additional criteria below. You must NOT run any tools "
        "yourself — consume this output only (Language Agnosticism).\n\n"
        "--- BEGIN CI OUTPUT ---\n"
        f"{ci_output}\n"
        "--- END CI OUTPUT ---\n\n"
        "=== ADDITIONAL CRITERIA (from CI output) ===\n"
        "- Q3 (Test Execution): Determine whether the test suite passes. If "
        "tests fail or error, report this as a coverage failure for the changed "
        "code (the changed production code is not exercised by passing tests).\n"
        "- Q4 (Coverage of Changed Code): Using the coverage report's "
        "missing-line information, check whether the production code lines "
        "added or modified in this diff are actually covered by passing tests. "
        "Report any changed production file/lines that are NOT covered. Do not "
        "flag test files themselves or files unchanged by this diff.\n"
        "- If the CI output above contains no coverage information (e.g., it is "
        "empty or only shows a collection error), evaluate Q1/Q2 from the diff "
        "alone and skip Q3/Q4.\n\n"
        "These additional criteria are scored under the same SCORING RULE above: "
        "any Q3/Q4 failure makes the overall verdict FAIL.\n"
    )


def _get_batch_budget() -> int:
    """Resolve the per-batch character budget, overridable via env var."""
    return int(os.getenv("REVIEW_BATCH_BUDGET_CHARS", str(BATCH_BUDGET_CHARS)))


def augment_judge_prompt(
    judge_key: str,
    prompt: str,
    syntax_result: tuple[bool, list[str], int] | None,
    arch_context: str,
    ci_coverage_output: str,
) -> str:
    """Apply judge-specific augmentations to a base judge prompt. Pure: no I/O.

    Extracted from ``main()`` so the per-judge augmentation dispatch (syntax
    verification, architecture context, CI coverage output) is unit-testable
    rather than buried in the untested entrypoint body (mirrors the
    ``entrypoint.py`` pattern of extracting logic out of ``main()``).

    Args:
        judge_key: the judge being augmented.
        prompt: the base system prompt for that judge.
        syntax_result: ``(passed, errors, checked)`` from
            :func:`verify_python_syntax`, or ``None`` when not run.
        arch_context: the loaded architecture context string (may be "").
        ci_coverage_output: the loaded CI coverage output string (may be "").

    Returns:
        The augmented prompt. Judges with no applicable augmentation
        (e.g. ``security``) receive the base prompt unchanged.
    """
    if judge_key == "syntax_lint" and syntax_result and syntax_result[2] > 0:
        syntax_passed, syntax_errors, _ = syntax_result
        if syntax_passed:
            prompt += (
                "\n\n=== DETERMINISTIC SYNTAX VERIFICATION ===\n"
                "All modified Python files have been programmatically verified "
                "via py_compile.\n"
                "Q1 (Syntax Validation) is PASS — do NOT flag syntax, "
                "indentation, or compilation issues.\n"
                "Focus your review on Q2 (JSON Schema) and "
                "Q3 (Naming Conventions)."
            )
        else:
            error_lines = "\n".join(f"- {e}" for e in syntax_errors)
            prompt += (
                "\n\n=== DETERMINISTIC SYNTAX VERIFICATION ===\n"
                "The following syntax errors were detected by py_compile:\n"
                f"{error_lines}\n"
                "Q1 (Syntax Validation) is FAIL based on deterministic "
                "verification. Report these as confirmed findings."
            )
    if judge_key == "architecture":
        if arch_context:
            prompt += "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\n" + arch_context
        else:
            prompt += "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\nNo specific architecture documentation found. Falling back to default rules."
    if judge_key == "test_coverage" and ci_coverage_output:
        prompt += build_test_coverage_ci_augmentation(ci_coverage_output)
    return prompt


def clip_chunk(chunk: str, budget: int) -> str:
    """Clip a single file's diff chunk to *budget* chars, appending a note when
    clipped. The note instructs the judge to return NEEDS REVIEW if it cannot
    fully evaluate the visible portion — preserving the strict-on-truncation
    semantics from the original ``truncate_diff`` (ADR-0023).
    """
    if len(chunk) > budget:
        return (
            chunk[:budget]
            + f"\n\n[NOTE: diff truncated to {budget} chars due to context limits. Evaluate the visible portion; return NEEDS REVIEW if you cannot fully evaluate.]"
        )
    return chunk


# Regex to detect the start of a per-file section in a unified diff.
# Lines look like: "diff --git a/foo.py b/foo.py"
_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)


def split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified ``git diff`` string into per-file sections.

    Returns a list of ``(filename, file_diff_section)`` pairs preserving the
    natural order of the diff. Each section starts at the ``diff --git`` line
    and includes all subsequent lines until the next ``diff --git`` or end of
    string.

    Edge cases:
      * Binary files (``Binary files ... differ``) — included as chunks with
        just the header lines; the judge trivially PASSes.
      * New/deleted/renamed files — included verbatim.
      * Leading text before the first ``diff --git`` (empty or whitespace) —
        discarded.
    """
    if not diff or not diff.strip():
        return []

    # Find all diff --git header positions.
    positions = [m.start() for m in _DIFF_FILE_HEADER_RE.finditer(diff)]
    if not positions:
        # No diff --git lines — treat the whole string as a single chunk with
        # an empty filename (defensive; shouldn't happen for real git diffs).
        return [("", diff)]

    chunks: list[tuple[str, str]] = []
    for i, pos in enumerate(positions):
        section = diff[pos : positions[i + 1]] if i + 1 < len(positions) else diff[pos:]
        section = section.rstrip("\n")
        if not section:
            continue
        filename = _extract_filename_from_section(section)
        chunks.append((filename, section))

    return chunks


def _extract_filename_from_section(section: str) -> str:
    """Extract the destination filename from a single ``diff --git`` section.

    The ``diff --git a/<path> b/<path>`` line is the first line. We parse the
    ``b/`` path (the destination), falling back to the ``a/`` path for deleted
    files where both sides are identical.
    """
    first_line = section.split("\n", 1)[0]
    # Format: "diff --git a/foo.py b/foo.py"
    # Also handle renames: "diff --git a/old.py b/new.py"
    tokens = first_line.split(" ")
    # tokens: ["diff", "--git", "a/foo.py", "b/foo.py"]
    if len(tokens) >= 4:
        b_path = tokens[-1]
        if b_path.startswith("b/"):
            return b_path[2:]
        # Deleted files or unusual formats — fall back to a/ path
        a_path = tokens[-2] if len(tokens) >= 4 else ""
        if a_path.startswith("a/"):
            return a_path[2:]
        return b_path
    return ""


def pack_into_batches(chunks: list[tuple[str, str]], budget: int) -> list[str]:
    """Pack per-file chunks into batch strings under a character budget.

    Files are packed in natural order (no size-based sorting, per ADR-0023).
    A single file exceeding the budget is clipped via :func:`clip_chunk` and
    becomes its own batch — preserving the strict-on-truncation gate for the
    pathological single-file case.

    Returns a list of batch strings, each containing one or more file sections
    joined by newlines. An empty input produces an empty list.
    """
    if not chunks:
        return []

    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for filename, section in chunks:
        section_len = len(section)

        if section_len > budget:
            # Flush current batch first.
            if current_parts:
                batches.append("\n".join(current_parts))
                current_parts = []
                current_len = 0
            # Clipped single file becomes its own batch.
            batches.append(clip_chunk(section, budget))
            continue

        if current_len + section_len + 1 > budget and current_parts:
            # Starting a new file would overflow — flush current batch.
            batches.append("\n".join(current_parts))
            current_parts = [section]
            current_len = section_len
        else:
            current_parts.append(section)
            current_len += section_len + 1  # +1 for the newline join

    if current_parts:
        batches.append("\n".join(current_parts))

    return batches


def verify_python_syntax(workspace_dir: str, diff: str) -> tuple[bool, list[str], int]:
    """Deterministically verify Python syntax of all modified .py files.

    Runs ``py_compile`` on each modified Python file found in the diff, using
    the workspace checkout. Returns ``(all_passed, errors, files_checked)``.

    Files that don't exist in the workspace (deleted files) are silently
    skipped. Non-Python files are not checked.
    """
    chunks = split_diff_by_file(diff)
    errors: list[str] = []
    files_checked = 0

    for filename, _ in chunks:
        if not filename.endswith(".py"):
            continue

        filepath = os.path.join(workspace_dir, filename)
        if not os.path.isfile(filepath):
            continue

        try:
            py_compile.compile(filepath, doraise=True)
            files_checked += 1
        except py_compile.PyCompileError as e:
            files_checked += 1
            errors.append(f"{filename}: {e}")

    return (len(errors) == 0, errors, files_checked)


JUDGE_KEYS = ["syntax_lint", "test_coverage", "architecture", "security"]

JUDGE_DISPLAY_NAMES = {
    "syntax_lint": "Syntax/Lint",
    "test_coverage": "Test Coverage",
    "architecture": "Architecture Compliance",
    "security": "Security",
}

JUDGE_PROMPTS = {
    "syntax_lint": SYSTEM_PROMPT_SYNTAX_LINT,
    "test_coverage": SYSTEM_PROMPT_TEST_COVERAGE,
    "architecture": SYSTEM_PROMPT_ARCH,
    "security": SYSTEM_PROMPT_SECURITY,
}


def _aggregate_verdicts(
    chunk_results: list[tuple[str, str, list[str], str | None, bool, str]],
) -> tuple[str, str, list[str], str | None, bool, str | None]:
    """Aggregate per-chunk judge results into a single judge verdict.

    Aggregation rules (ADR-0023):
      - status: FAIL if any chunk FAIL; NEEDS REVIEW if any chunk NEEDS REVIEW
        (and none FAIL); PASS only if all chunks PASS.
      - reasoning: concatenate with ``--- Chunk N: <filename> ---`` separators.
      - findings: concatenate all chunks' findings lists.
      - used_fallback: True if any chunk used the fallback model.
      - final_model: the fallback model if any chunk fell back, else the
        primary model (worst-case reporting so the Fallback Indicator is
        surfaced when any chunk degraded).
      - error: first error encountered; subsequent errors appear in reasoning.

    Args:
        chunk_results: list of (status, reasoning, findings, error,
            used_fallback, final_model) tuples, one per chunk.

    Returns:
        Aggregated (status, reasoning, findings, error, used_fallback,
        final_model) tuple.
    """
    if not chunk_results:
        return "PASS", "", [], None, False, None

    if len(chunk_results) == 1:
        return chunk_results[0]

    agg_status = "PASS"
    all_findings: list[str] = []
    reasoning_parts: list[str] = []
    first_error = None
    any_fallback = False
    fallback_model = None
    primary_model = None

    for i, (
        c_status,
        c_reasoning,
        c_findings,
        c_error,
        c_fallback,
        c_model,
    ) in enumerate(chunk_results):
        if c_status == "FAIL":
            agg_status = "FAIL"
        elif c_status == "NEEDS REVIEW" and agg_status != "FAIL":
            agg_status = "NEEDS REVIEW"

        all_findings.extend(c_findings)

        label = f"--- Chunk {i + 1} ---"
        if c_reasoning:
            reasoning_parts.append(f"{label}\n{c_reasoning}")
        elif c_error:
            reasoning_parts.append(f"{label}\nException: {c_error}")

        if c_error and first_error is None:
            first_error = c_error

        if c_fallback:
            any_fallback = True
            fallback_model = c_model
        elif primary_model is None:
            primary_model = c_model

    final_model = fallback_model if any_fallback else primary_model
    combined_reasoning = "\n\n".join(reasoning_parts)

    return (
        agg_status,
        combined_reasoning,
        all_findings,
        first_error,
        any_fallback,
        final_model,
    )


def _enrich_chunk(chunk_diff: str, workspace_dir: str) -> str:
    """Enrich a single diff chunk with enclosing function context.

    Uses enrich_diff_with_function_context from scripts/enrichment.py.
    Applied per-chunk so each file's context stays with its diff segment
    (avoids ADR-0023's pooled-format orphan bug).
    """
    from enrichment import enrich_diff_with_function_context

    return enrich_diff_with_function_context(chunk_diff, workspace_dir)


def run_judge(judge_key, prompt, diff, api_key, llm_caller=call_llm_for_review):
    """Runs a single judge evaluation, returning (status, reasoning, findings,
    error, used_fallback, final_model).

    Evaluation path (ADR-0023):
      1. **Empty diff** → short-circuit to PASS, no LLM call.
      2. **Fast path** (diff ≤ batch budget) → single LLM call, no splitting,
         no aggregation. Zero behavioral change for normal PRs.
      3. **Multi-batch path** (diff > budget) → split per-file, pack into
         batches under the budget, one LLM call per batch, aggregate verdicts.

    Each chunk receives the full ADR-0021 retry/fallback treatment via
    ``llm_caller``. Aggregation: FAIL in any chunk → judge FAIL; any NEEDS
    REVIEW → judge NEEDS REVIEW; all PASS → judge PASS.

    status is normalized to uppercase ('PASS', 'FAIL', 'NEEDS REVIEW').
    On an exception the judge returns 'NEEDS REVIEW' with the error captured.
    used_fallback and final_model are False/None on error paths.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(f"{judge_key}_evaluation") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        cfg = resolve_model_config(judge_key)
        span.set_attribute(LLM_MODEL_NAME, cfg["model"])
        span.set_attribute("eval.dimension", judge_key)

        budget = _get_batch_budget()

        # 1. Empty diff — short-circuit to PASS.
        if not diff or not diff.strip():
            span.set_attribute("eval.chunk_count", 0)
            span.set_attribute("eval.diff_total_chars", 0)
            span.set_attribute("eval.verdict", "PASS")
            span.set_attribute("eval.findings_count", 0)
            span.set_attribute("used_fallback", False)
            span.set_attribute("final_model", cfg["model"])
            span.set_status(trace.Status(trace.StatusCode.OK))
            return "PASS", "", [], None, False, cfg["model"]

        # 2. Fast path — diff fits in one batch, no splitting.
        if len(diff) <= budget:
            span.set_attribute("eval.chunk_count", 1)
            span.set_attribute("eval.diff_total_chars", len(diff))
            span.set_attribute("eval.workspace_dir", os.getenv("GITHUB_WORKSPACE", "."))
            enriched_diff = _enrich_chunk(diff, os.getenv("GITHUB_WORKSPACE", "."))
            status, reasoning, findings, error, used_fallback, final_model = (
                _run_single_chunk(
                    judge_key,
                    prompt,
                    enriched_diff,
                    api_key,
                    llm_caller,
                    span,
                    cfg["model"],
                )
            )
            _set_judge_span_attributes(
                span, status, findings, used_fallback, final_model
            )
            return status, reasoning, findings, error, used_fallback, final_model

        # 3. Multi-batch path — split per-file, pack, iterate, aggregate.
        chunks = split_diff_by_file(diff)
        batches = pack_into_batches(chunks, budget)

        span.set_attribute("eval.chunk_count", len(batches))
        span.set_attribute("eval.diff_total_chars", len(diff))

        log(
            f"[INFO] Judge {judge_key}: multi-batch path, "
            f"{len(chunks)} files → {len(batches)} batches (budget={budget})"
        )

        chunk_results: list[tuple[str, str, list[str], str | None, bool, str]] = []
        for batch in batches:
            enriched_batch = _enrich_chunk(batch, os.getenv("GITHUB_WORKSPACE", "."))
            result = _run_single_chunk(
                judge_key,
                prompt,
                enriched_batch,
                api_key,
                llm_caller,
                span,
                cfg["model"],
            )
            chunk_results.append(result)

        status, reasoning, findings, error, used_fallback, final_model = (
            _aggregate_verdicts(chunk_results)
        )
        _set_judge_span_attributes(span, status, findings, used_fallback, final_model)
        return status, reasoning, findings, error, used_fallback, final_model


def _run_single_chunk(
    judge_key: str,
    prompt: str,
    chunk_diff: str,
    api_key: str,
    llm_caller,
    span,
    default_model: str,
) -> tuple[str, str, list[str], str | None, bool, str]:
    """Evaluate a single diff chunk via ``llm_caller`` and return a result tuple.

    Catches exceptions and converts them to a NEEDS REVIEW verdict with the
    error captured, mirroring the original ``run_judge`` error handling.
    """
    reasoning = ""
    findings: list[str] = []
    error = None
    status = "NEEDS REVIEW"
    used_fallback = False
    final_model = default_model

    try:
        raw_resp, metadata = llm_caller(judge_key, prompt, chunk_diff, api_key)
        used_fallback = metadata.get("used_fallback", False)
        final_model = metadata.get("final_model", default_model)
        verdict, reasoning, findings = evaluate_response(raw_resp)
        if verdict == "Pass":
            status = "PASS"
        elif verdict == "Fail":
            status = "FAIL"
        else:
            status = "NEEDS REVIEW"
    except Exception as e:
        log(f"[ERR] Judge {judge_key} chunk failed: {e}")
        status = "NEEDS REVIEW"
        error = str(e)
        reasoning = f"Exception encountered: {e}"
        span.record_exception(e)

    return status, reasoning, findings, error, used_fallback, final_model


def _set_judge_span_attributes(span, status, findings, used_fallback, final_model):
    """Set the common span attributes for a judge evaluation."""
    span.set_attribute("eval.verdict", status)
    span.set_attribute("eval.findings_count", len(findings))
    span.set_attribute("used_fallback", used_fallback)
    span.set_attribute("final_model", final_model)
    status_code = trace.StatusCode.OK if status == "PASS" else trace.StatusCode.ERROR
    span.set_status(
        trace.Status(
            status_code,
            f"Verdict: {status}" if status_code == trace.StatusCode.ERROR else None,
        )
    )


def build_review_body(judges_data: dict) -> str:
    """Builds the combined GitHub review body (pure helper, no I/O).

    Renders a human-readable summary table, per-judge detail sections, and a
    hidden machine-parseable verdict block (HTML comment) at the very end.
    """
    report_lines = []
    report_lines.append("### 🤖 Automated LLM PR Judges Summary\n")
    report_lines.append("| Judge | Status | Details |")
    report_lines.append("| :--- | :---: | :--- |")

    for key in JUDGE_KEYS:
        info = judges_data[key]
        status = info["status"]
        if status == "PASS":
            status_emoji = "✅ PASS"
        elif status == "FAIL":
            status_emoji = "❌ FAIL"
        else:
            status_emoji = "⚠️ NEEDS REVIEW"

        if status == "PASS":
            details = "All criteria passed."
        elif status == "FAIL":
            count = len(info["findings"])
            details = f"{count} violation{'s' if count != 1 else ''} found."
        else:
            if info.get("error"):
                details = f"Check failed to run: {info['error']}"
            else:
                details = "Insufficient context."

        report_lines.append(
            f"| **{info['name']} (`{key}`)** | {status_emoji} | {details} |"
        )

    report_lines.append("\n---\n")

    for key in JUDGE_KEYS:
        info = judges_data[key]
        report_lines.append(f"### ➡️ {info['name']} (`{key}`)")
        if info["status"] == "PASS":
            status_emoji = "✅ PASS"
        elif info["status"] == "FAIL":
            status_emoji = "❌ FAIL"
        else:
            status_emoji = "⚠️ NEEDS REVIEW"
        report_lines.append(f"* **Status**: {status_emoji}")

        if info.get("used_fallback"):
            report_lines.append(
                f"\n> ⚠️ **Fallback Model Used**: This verdict was produced by "
                f"`{info.get('final_model', 'unknown')}` after the primary model "
                f"returned empty responses."
            )

        if info["findings"]:
            report_lines.append("\n#### 📝 Detailed Findings:")
            for f in info["findings"]:
                sev, msg = f.split("|", 1) if "|" in f else ("bug", f)
                report_lines.append(f"- `[{sev.upper()}]` {msg}")

        if info.get("error"):
            report_lines.append(f"\n⚠️ **Execution Error**: {info['error']}")

        if info["reasoning"]:
            report_lines.append("\n#### 🧠 Reasoning:")
            report_lines.append("<details>")
            report_lines.append("<summary>Reasoning Details</summary>\n")
            report_lines.append(info["reasoning"])
            report_lines.append("\n</details>")

        report_lines.append("\n---\n")

    combined_report = "\n".join(report_lines)

    # Hidden machine-parseable verdict block (invisible in GitHub rendering).
    hidden_lines = ["<!-- llm-pr-review-verdicts"]
    for key in JUDGE_KEYS:
        hidden_lines.append(f"{key}: {judges_data[key]['status']}")
    hidden_lines.append("-->")
    combined_report += "\n" + "\n".join(hidden_lines)

    return combined_report


def main():
    # Initialize telemetry
    init_telemetry()

    pr_number = os.getenv("PR_NUMBER", "")
    if not pr_number:
        sys.stderr.write("[ERR] PR_NUMBER not set\n")
        sys.exit(1)

    gh_pat = os.getenv("GH_PAT", "")
    gh_token = os.getenv("GH_TOKEN", "")
    token = gh_pat if gh_pat else gh_token
    if not token:
        sys.stderr.write("[ERR] GitHub token not configured.\n")
        sys.exit(1)

    os.environ["GH_TOKEN"] = token

    diff = sys.stdin.read()
    log(f"[INFO] Diff length: {len(diff)}")

    tracer = get_tracer()
    with tracer.start_as_current_span("pr_review") as main_span:
        main_span.set_attribute(OPENINFERENCE_SPAN_KIND, "CHAIN")
        main_span.set_attribute(
            INPUT_VALUE, json.dumps({"pr_number": pr_number, "diff_len": len(diff)})
        )

        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            sys.stderr.write("[ERR] OPENROUTER_API_KEY not configured.\n")
            sys.exit(1)

        judges_data: dict[str, Any] = {}
        for judge_key in JUDGE_KEYS:
            judges_data[judge_key] = {
                "name": JUDGE_DISPLAY_NAMES[judge_key],
                "prompt": JUDGE_PROMPTS[judge_key],
                "status": None,
                "reasoning": "",
                "findings": [],
                "error": None,
                "used_fallback": False,
                "final_model": None,
            }

        workspace_dir = os.getenv("GITHUB_WORKSPACE", ".")
        syntax_passed, syntax_errors, syntax_checked = verify_python_syntax(
            workspace_dir, diff
        )
        if syntax_checked > 0:
            log(
                f"[INFO] Deterministic syntax check: {syntax_checked} Python files "
                f"checked, {'all passed' if syntax_passed else f'{len(syntax_errors)} errors'}"
            )

        ci_coverage_output = load_ci_coverage_output()
        if ci_coverage_output:
            log(
                f"[INFO] CI coverage output loaded ({len(ci_coverage_output)} chars) "
                f"for test_coverage judge"
            )

        # Load once: architecture context is identical across judge iterations.
        arch_context = load_architecture_context(workspace_dir)
        syntax_result = (syntax_passed, syntax_errors, syntax_checked)

        for judge_key in JUDGE_KEYS:
            judge_info = judges_data[judge_key]
            prompt = augment_judge_prompt(
                judge_key,
                judge_info["prompt"],
                syntax_result,
                arch_context,
                ci_coverage_output,
            )

            log(f"[INFO] Running judge: {judge_key}")
            status, reasoning, findings, error, used_fallback, final_model = run_judge(
                judge_key, prompt, diff, openrouter_api_key
            )
            judge_info["status"] = status
            judge_info["reasoning"] = reasoning
            judge_info["findings"] = findings
            judge_info["error"] = error
            judge_info["used_fallback"] = used_fallback
            judge_info["final_model"] = final_model

        body = build_review_body(judges_data)
        review_action = (
            "approve"
            if all(judges_data[k]["status"] == "PASS" for k in JUDGE_KEYS)
            else "request-changes"
        )
        try:
            submit_github_review(pr_number, review_action, body)
        except Exception as e:
            log(f"[ERR] Failed to submit GitHub review: {e}")
            sys.exit(1)

        any_failed = any(
            judges_data[k]["status"] in ("FAIL", "NEEDS REVIEW") for k in JUDGE_KEYS
        )
        if any_failed:
            log("[ERR] LLM review found issues in one or more judges")
            main_span.set_status(
                trace.Status(trace.StatusCode.ERROR, "Review evaluation failed")
            )
            sys.exit(1)
        else:
            log("[INFO] LLM review completed successfully")
            main_span.set_status(trace.Status(trace.StatusCode.OK))
            sys.exit(0)


if __name__ == "__main__":
    main()
