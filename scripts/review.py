import os
import sys
import json
import datetime
import urllib.request
import urllib.error
import subprocess
import tempfile
import time
from typing import List, Tuple

from telemetry import (
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
    "- Do NOT flag intentional, safe usages of low-level commands that are thoroughly sanitised.\n\n"
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
    "- Do NOT flag intentional scaffolding that is explicitly requested in the issue requirements.\n\n"
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



def run_command(cmd, env=None):
    """Runs a shell command and returns code, stdout, stderr."""
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return res.returncode, res.stdout, res.stderr

ROUTING_PREFERENCES = {
    "moonshotai/kimi-k2.7-code": {
        "only": ["together", "moonshotai"],
        "allow_fallbacks": True,
        "sort": "latency"
    },
    "deepseek/deepseek-v4-pro": {
        "only": ["baidu", "novita", "deepinfra"],
        "allow_fallbacks": True,
        "sort": "latency"
    },
    "deepseek/deepseek-v4-flash": {
        "only": ["deepinfra", "baidu", "novita"],
        "allow_fallbacks": True,
        "sort": "latency"
    }
}

def call_openrouter_api(model, messages, api_key):
    """Performs HTTP request to OpenRouter chat completions API."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    payload_dict = {"model": model, "messages": messages}
    if model in ROUTING_PREFERENCES:
        payload_dict["provider"] = ROUTING_PREFERENCES[model]
        
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

def call_llm_for_review(model, system_prompt, diff, api_key):
    """Wraps OpenRouter API call in a trace span and executes it with retry logic."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": diff}
    ]
    
    tracer = get_tracer()
    with tracer.start_as_current_span("openrouter_chat_completion") as span:
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        span.set_attribute(LLM_MODEL_NAME, model)
        span.set_attribute(INPUT_VALUE, json.dumps(messages))
        
        response_body = ""
        last_error = ""
        
        for attempt in range(2):
            try:
                status, body = call_openrouter_api(model, messages, api_key)
                
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
    
    review_model = os.getenv("REVIEW_MODEL", "moonshotai/kimi-k2.7-code")
    log(f"[INFO] Review model: {review_model}")
    
    diff = sys.stdin.read()
    if not diff.strip():
        log("[WARN] No diff to review")
        sys.exit(0)
        
    tracer = get_tracer()
    with tracer.start_as_current_span("pr_review") as main_span:
        main_span.set_attribute(OPENINFERENCE_SPAN_KIND, "CHAIN")
        main_span.set_attribute(INPUT_VALUE, json.dumps({"pr_number": pr_number, "diff_len": len(diff)}))
        
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            sys.stderr.write("[ERR] OPENROUTER_API_KEY not configured.\n")
            sys.exit(1)
            
        # 1. RUN SECURITY EVALUATION
        sec_verdict = "Needs Review"
        sec_reasoning = "Evaluation failed to run"
        sec_findings = []
        
        with tracer.start_as_current_span("security_evaluation") as sec_span:
            sec_span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
            sec_span.set_attribute(LLM_MODEL_NAME, review_model)
            sec_span.set_attribute("eval.dimension", "security")
            
            try:
                raw_sec_resp = call_llm_for_review(review_model, SYSTEM_PROMPT_SECURITY, diff, openrouter_api_key)
                sec_verdict, sec_reasoning, sec_findings = evaluate_response(raw_sec_resp)
            except Exception as e:
                log(f"[ERR] Security LLM call failed: {e}")
                sec_reasoning = f"Exception encountered: {e}"
                sec_span.record_exception(e)
                
            sec_span.set_attribute("eval.verdict", sec_verdict)
            sec_span.set_attribute("eval.reasoning", sec_reasoning)
            sec_span.set_attribute("eval.findings_count", len(sec_findings))
            
            status_code = trace.StatusCode.OK if sec_verdict == "Pass" else trace.StatusCode.ERROR
            sec_span.set_status(trace.Status(status_code, f"Verdict: {sec_verdict}" if status_code == trace.StatusCode.ERROR else None))
            
        # 2. RUN ARCHITECTURE COMPLIANCE EVALUATION
        arch_verdict = "Needs Review"
        arch_reasoning = "Evaluation failed to run"
        arch_findings = []
        
        with tracer.start_as_current_span("architecture_evaluation") as arch_span:
            arch_span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
            arch_span.set_attribute(LLM_MODEL_NAME, review_model)
            arch_span.set_attribute("eval.dimension", "architecture_compliance")
            
            workspace_dir = os.getenv("GITHUB_WORKSPACE", ".")
            arch_context = load_architecture_context(workspace_dir)
            
            if arch_context:
                arch_prompt = SYSTEM_PROMPT_ARCH + "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\n" + arch_context
            else:
                arch_prompt = SYSTEM_PROMPT_ARCH + "\n\n=== REPOSITORY ARCHITECTURE CONTEXT ===\nNo specific architecture documentation found. Falling back to default rules."
                
            try:
                raw_arch_resp = call_llm_for_review(review_model, arch_prompt, diff, openrouter_api_key)
                arch_verdict, arch_reasoning, arch_findings = evaluate_response(raw_arch_resp)
            except Exception as e:
                log(f"[ERR] Architecture Compliance LLM call failed: {e}")
                arch_reasoning = f"Exception encountered: {e}"
                arch_span.record_exception(e)
                
            arch_span.set_attribute("eval.verdict", arch_verdict)
            arch_span.set_attribute("eval.reasoning", arch_reasoning)
            arch_span.set_attribute("eval.findings_count", len(arch_findings))
            
            status_code = trace.StatusCode.OK if arch_verdict == "Pass" else trace.StatusCode.ERROR
            arch_span.set_status(trace.Status(status_code, f"Verdict: {arch_verdict}" if status_code == trace.StatusCode.ERROR else None))
            
        # 3. POST DISCRETE REVIEWS AND COMPUTE EXIT CODE
        security_failed = (sec_verdict in ["Fail", "Needs Review"])
        arch_failed = (arch_verdict in ["Fail", "Needs Review"])
        
        if not security_failed and not arch_failed:
            # Both passed! Submit single approval review
            body = "### LLM PR Review: PASS\n\nAll automated checks passed.\n\n"
            body += f"#### Security reasoning:\n{sec_reasoning}\n\n"
            body += f"#### Architecture Compliance reasoning:\n{arch_reasoning}\n"
            try:
                submit_github_review(pr_number, "approve", body)
            except Exception as e:
                log(f"[ERR] Failed to submit approval review: {e}")
                sys.exit(1)
        else:
            # At least one failed
            if security_failed:
                action = "request-changes"
                body = f"### LLM PR Review - Security: {sec_verdict.upper()}\n\n"
                body += f"#### Reasoning:\n{sec_reasoning}\n\n"
                if sec_findings:
                    body += "#### Findings:\n"
                    for f in sec_findings:
                        sev, msg = f.split("|", 1)
                        body += f"- [{sev}] {msg}\n"
                else:
                    body += "No findings listed (e.g. LLM refused or had missing evidence).\n"
                try:
                    submit_github_review(pr_number, action, body)
                except Exception as e:
                    log(f"[ERR] Failed to submit Security review: {e}")
                    sys.exit(1)
                    
            if arch_failed:
                action = "comment"
                body = f"### LLM PR Review - Architecture Compliance: {arch_verdict.upper()}\n\n"
                body += f"#### Reasoning:\n{arch_reasoning}\n\n"
                if arch_findings:
                    body += "#### Findings:\n"
                    for f in arch_findings:
                        sev, msg = f.split("|", 1)
                        body += f"- [{sev}] {msg}\n"
                else:
                    body += "No findings listed (e.g. LLM refused or context was missing).\n"
                try:
                    submit_github_review(pr_number, action, body)
                except Exception as e:
                    log(f"[ERR] Failed to submit Architecture Compliance review: {e}")
                    sys.exit(1)
                    
        # Exits non-zero if Security failed or Architecture Compliance failed (so CI checks fail and block PR merge)
        if security_failed or arch_failed:
            log("[ERR] LLM review found security, architecture compliance, or evidence issues")
            main_span.set_status(trace.Status(trace.StatusCode.ERROR, "Review evaluation failed"))
            sys.exit(1)
        else:
            log("[INFO] LLM review completed successfully")
            main_span.set_status(trace.Status(trace.StatusCode.OK))
            sys.exit(0)

if __name__ == "__main__":
    main()
