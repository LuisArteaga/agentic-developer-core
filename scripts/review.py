import os
import sys
import json
import datetime
import urllib.request
import urllib.error
import subprocess
import tempfile
import time
from typing import List, Tuple, Dict, Any

# Add project root and scripts dir to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

from orchestrator.config import resolve_model_config  # noqa: E402

from telemetry import (  # noqa: E402
    init_telemetry,
    get_tracer,
    trace,
    OPENINFERENCE_SPAN_KIND,
    INPUT_VALUE,
    OUTPUT_VALUE,
    LLM_MODEL_NAME,
    TOOL_NAME,
    TOOL_PARAMETERS
)

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
        sys.stdout.write(f"[WARN] Log directory {log_dir} is not writeable (Permission Denied). Falling back to container /tmp/agent_logs\n")
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
    '{"severity": "error", "message": "[QX] Details of the failure"}.\n'
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
    '{"severity": "error", "message": "[QX] Details of the failure"}.\n'
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
    "- FAIL: If any compliance deviation is found. Report each as a JSON object on a single line inside the findings block: {\"severity\": \"bug\", \"message\": \"...\"}.\n"
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
    "- FAIL: If one or more verified security vulnerabilities are found. Report each as a JSON object on a single line inside the findings block: {\"severity\": \"security\", \"message\": \"...\"}.\n"
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

MAX_DIFF_CHARS = 100000


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
    payload_dict: Dict[str, Any] = {
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

def call_openrouter_api(model, messages, api_key, routing=None, temperature=0.0, options=None):
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
            "User-Agent": "agentic-developer-core/1.0"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as response:
        return response.status, response.read().decode("utf-8")

def call_llm_for_review(judge_key, system_prompt, diff, api_key):
    """Resolves config for judge_key, wraps OpenRouter API call in a trace span and executes it with retry logic."""
    cfg = resolve_model_config(judge_key)
    model = cfg["model"]
    routing = cfg["routing"]
    temperature = cfg["temperature"]
    options = cfg["options"]
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": diff}
    ]
    
    tracer = get_tracer()
    with tracer.start_as_current_span("openrouter_chat_completion") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        span.set_attribute(LLM_MODEL_NAME, model)
        span.set_attribute(INPUT_VALUE, json.dumps(messages))
        log(f"[INFO] Running judge {judge_key} using model: {model}")
        
        response_body = ""
        last_error = ""
        
        for attempt in range(2):
            try:
                status, body = call_openrouter_api(model, messages, api_key, routing=routing, temperature=temperature, options=options)
                
                # Pre-validate structure before considering it OK
                parsed_body = json.loads(body, strict=False)
                
                if "error" in parsed_body:
                    err = parsed_body["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise Exception(f"OpenRouter API error: {msg}")
                elif "choices" not in parsed_body or not parsed_body["choices"]:
                    raise Exception("OpenRouter response missing choices block")
                
                response_body = body
                break
            except Exception as e:
                last_error = str(e)
                log(f"[WARN] OpenRouter attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    time.sleep(3)
                    continue
                raise Exception(f"LLM review failed after retries. Last error: {last_error}")
        
        span.set_attribute(OUTPUT_VALUE, response_body)
        return response_body

def submit_github_review(pr_number, action, body_content):
    """Submits findings using GitHub CLI wrapped in a trace span."""
    tracer = get_tracer()
    with tracer.start_as_current_span("submit_github_review") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "TOOL")
        span.set_attribute(TOOL_NAME, "submit_github_review")
        span.set_attribute(TOOL_PARAMETERS, json.dumps({
            "pr_number": pr_number,
            "action": action,
            "body_content": body_content
        }))
        span.set_attribute(INPUT_VALUE, json.dumps({
            "pr_number": pr_number,
            "action": action,
            "body_content": body_content
        }))
        
        # Get PR Author
        pr_author_cmd = ["gh", "pr", "view", pr_number, "--json", "author", "--jq", ".author.login"]
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
            review_cmd = ["gh", "pr", "review", pr_number, action_flag, "--body-file", temp_path]
            ret, stdout, stderr = run_command(review_cmd)
            
            span.set_attribute(OUTPUT_VALUE, json.dumps({
                "exit_code": ret,
                "stdout": stdout,
                "stderr": stderr
            }))
            
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
    block = text[last_open + len(open_tag):]
    close_idx = block.find(close_tag)
    if close_idx != -1:
        return block[:close_idx].strip()
    return block.strip()

def evaluate_response(raw_response: str) -> Tuple[str, str, List[str]]:
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
        return "Needs Review", "Response lacks both <reasoning> and <findings> tags. Original output:\n" + content, []
        
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
        sys.stdout.write(f"[WARN] Architecture context file {context_file} is missing.\n")
        
    # Try reading adr/*.md files
    adr_dir = os.path.join(docs_dir, "adr")
    if os.path.isdir(adr_dir):
        try:
            for entry in sorted(os.listdir(adr_dir)):
                if entry.endswith(".md"):
                    entry_path = os.path.join(adr_dir, entry)
                    if os.path.isfile(entry_path):
                        with open(entry_path, "r", encoding="utf-8", errors="replace") as f:
                            context_lines.append(f"--- docs/adr/{entry} ---")
                            context_lines.append(f.read())
                            context_lines.append("")
        except Exception as e:
            sys.stdout.write(f"[WARN] Failed to read ADR files from {adr_dir}: {e}\n")
    else:
        sys.stdout.write(f"[WARN] Architecture Decision Records folder {adr_dir} is missing.\n")
        
    return "\n".join(context_lines)

def truncate_diff(diff: str) -> str:
    """Truncates the diff to REVIEW_MAX_DIFF_CHARS chars, appending a note when truncated."""
    max_chars = int(os.getenv("REVIEW_MAX_DIFF_CHARS", str(MAX_DIFF_CHARS)))
    if len(diff) > max_chars:
        return diff[:max_chars] + f"\n\n[NOTE: diff truncated to {max_chars} chars due to context limits. Evaluate the visible portion; return NEEDS REVIEW if you cannot fully evaluate.]"
    return diff

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

def run_judge(judge_key, prompt, diff, api_key, llm_caller=call_llm_for_review):
    """Runs a single judge evaluation, returning (status, reasoning, findings, error).

    status is normalized to uppercase ('PASS', 'FAIL', 'NEEDS REVIEW').
    On an exception the judge returns 'NEEDS REVIEW' with the error captured.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(f"{judge_key}_evaluation") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        cfg = resolve_model_config(judge_key)
        span.set_attribute(LLM_MODEL_NAME, cfg["model"])
        span.set_attribute("eval.dimension", judge_key)

        reasoning = ""
        findings = []
        error = None
        status = "NEEDS REVIEW"

        try:
            raw_resp = llm_caller(judge_key, prompt, diff, api_key)
            verdict, reasoning, findings = evaluate_response(raw_resp)
            if verdict == "Pass":
                status = "PASS"
            elif verdict == "Fail":
                status = "FAIL"
            else:
                status = "NEEDS REVIEW"
        except Exception as e:
            log(f"[ERR] Judge {judge_key} failed: {e}")
            status = "NEEDS REVIEW"
            error = str(e)
            reasoning = f"Exception encountered: {e}"
            span.record_exception(e)

        span.set_attribute("eval.verdict", status)
        span.set_attribute("eval.findings_count", len(findings))
        status_code = trace.StatusCode.OK if status == "PASS" else trace.StatusCode.ERROR
        span.set_status(trace.Status(status_code, f"Verdict: {status}" if status_code == trace.StatusCode.ERROR else None))

        return status, reasoning, findings, error

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
            details = f"{len(info['findings'])} violations found."
        else:
            if info.get("error"):
                details = f"Check failed to run: {info['error']}"
            else:
                details = "Insufficient context."

        report_lines.append(f"| **{info['name']} (`{key}`)** | {status_emoji} | {details} |")

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
    diff = truncate_diff(diff)
    log(f"[INFO] Diff length: {len(diff)}")
    
    tracer = get_tracer()
    with tracer.start_as_current_span("pr_review") as main_span:
        main_span.set_attribute(OPENINFERENCE_SPAN_KIND, "CHAIN")
        main_span.set_attribute(INPUT_VALUE, json.dumps({"pr_number": pr_number, "diff_len": len(diff)}))
        
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            sys.stderr.write("[ERR] OPENROUTER_API_KEY not configured.\n")
            sys.exit(1)
            
        judges_data: Dict[str, Any] = {}
        for judge_key in JUDGE_KEYS:
            judges_data[judge_key] = {
                "name": JUDGE_DISPLAY_NAMES[judge_key],
                "prompt": JUDGE_PROMPTS[judge_key],
                "status": None,
                "reasoning": "",
                "findings": [],
                "error": None,
            }
        
        for judge_key in JUDGE_KEYS:
            judge_info = judges_data[judge_key]
            prompt = judge_info["prompt"]
            if judge_key == "architecture":
                workspace_dir = os.getenv("GITHUB_WORKSPACE", ".")
                arch_context = load_architecture_context(workspace_dir)
                if arch_context:
                    prompt += "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\n" + arch_context
                else:
                    prompt += "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\nNo specific architecture documentation found. Falling back to default rules."
            
            log(f"[INFO] Running judge: {judge_key}")
            status, reasoning, findings, error = run_judge(
                judge_key, prompt, diff, openrouter_api_key
            )
            judge_info["status"] = status
            judge_info["reasoning"] = reasoning
            judge_info["findings"] = findings
            judge_info["error"] = error
        
        body = build_review_body(judges_data)
        review_action = "approve" if all(judges_data[k]["status"] == "PASS" for k in JUDGE_KEYS) else "request-changes"
        try:
            submit_github_review(pr_number, review_action, body)
        except Exception as e:
            log(f"[ERR] Failed to submit GitHub review: {e}")
            sys.exit(1)
        
        any_failed = any(judges_data[k]["status"] in ("FAIL", "NEEDS REVIEW") for k in JUDGE_KEYS)
        if any_failed:
            log("[ERR] LLM review found issues in one or more judges")
            main_span.set_status(trace.Status(trace.StatusCode.ERROR, "Review evaluation failed"))
            sys.exit(1)
        else:
            log("[INFO] LLM review completed successfully")
            main_span.set_status(trace.Status(trace.StatusCode.OK))
            sys.exit(0)

if __name__ == "__main__":
    main()
