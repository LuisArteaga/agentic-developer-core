# ADR 0012: Structured JSON Planning and Pre-Flight Path Safety Validation

## Status
Accepted

## Context
The orchestrator's `Plan-Node` (introduced in Issue #10) is responsible for analyzing a claimed GitHub issue alongside the codebase's directory structure to generate a step-by-step development plan. However, this phase introduces two primary challenges:

1. **Indirect Prompt Injection (Security Threat)**:
   GitHub issue titles and bodies are untrusted, attacker-controlled inputs. A malicious actor could open an issue containing prompt injection payloads designed to override the system prompt and steer the planner to access sensitive configuration files (such as `.env`, private SSH keys, or cloud credentials) or inject backdoors into critical source files.
   
2. **Planning Ambiguity and Hallucinations (Reliability Threat)**:
   Traditional free-form natural language (Markdown) planning is highly flexible but prone to hallucinations, path errors, and lack of structure. The downstream ReAct Worker agent requires unambiguous instructions, and the orchestrator needs to programmatically track progress.

According to recent academic research, such as *Routine: A Structural Planning Framework (2025)* and *Structuring the Unstructured: Multi-Agent Framework (2025)*, unstructured LLM planning often fails to achieve production-grade reliability. These papers demonstrate that using a **structured intermediate representation (IR)**—like a schema-enforced script or JSON payload—drastically improves execution accuracy, reducing planning hallucinations and enabling deterministic validation before any code-writing execution begins.

Additionally, ADR-0006 establishes the **Read-Before-Edit** safety constraint and mandates that "the record of read files is reset at the start of each phase and upon stateful resume." The transition into the planning phase must therefore clear this state to prevent stale access permissions from carrying over.

## Decision
We will implement a structured, highly secure, and compliant planning node in `orchestrator/nodes.py`:

1. **Structured JSON Planning via Pydantic**:
   We will enforce a strict JSON output schema using LangChain's `.with_structured_output()` bound to a Pydantic `DevelopmentPlan` model. The plan is decomposed into a list of explicit `PlanningTask` steps containing the step number, target files, action types (`read`, `patch`, `verify`), and clear instructions.
   
2. **Prompt Injection Hardening**:
   Untrusted GitHub API inputs are wrapped inside explicit XML-like tags (`<issue_title>` and `<issue_body>`) in the prompt. We inject strict system-level instructions telling the LLM to treat content inside these tags strictly as untrusted data, never as directives to execute, and to restrict planning to the target codebase.
   
3. **Pre-Flight Path Safety Validation (Defense-in-Depth)**:
   We will implement a pure Python, zero-dependency helper `_is_safe_path` that validates all target files in the generated plan *before* the planning phase concludes. It programmatically blocks absolute paths, directory traversals (`..`), sensitive directories (`.git`, `.venv`, `.agent_logs`, `.agents`, `node_modules`), credential filenames (`.env`, `passwd`, `shadow`, private keys like `id_rsa`), and certificate extensions (`.pem`, `.key`, `.pfx`, `.pkcs12`).
   If any task in the generated plan contains an unsafe path, the node raises a `ValueError` ("Security Block"), fails fast, and marks the state as `failed` at the `planning` phase, preventing execution.
   
4. **ADR-0006 Compliance**:
   At the start of `plan_node`, the state's `read_files` tracking list is explicitly cleared to `[]`, ensuring compliance with the phase-reset safety invariant.
   
5. **State Serialization**:
   The validated plan is serialized to a JSON string and saved directly inside the graph state's `plan` field, persisting atomically to `.agent_logs/state.json` (complying with ADR-0008).

## Consequences

### Pros
- **High Security**: Attackers cannot exploit indirect prompt injection to steer the agent into exfiltrating secrets or modifying unauthorized system files, as all target paths are programmatically validated before execution.
- **Improved Reliability**: Enforcing a strict schema prevents LLM hallucinations of task structure, ensuring the Worker agent receives structured, actionable inputs.
- **Pre-Flight Validation**: Allows the orchestrator to fail fast on invalid or malicious plans before launching the resource-heavy Worker agent.
- **True Stateful Resume**: If planning fails (due to LLM errors or security blocks), the state is persisted as `failed` at `planning`, allowing clean, automated resume from the same phase once resolved.
- **ADR-0006 Alignment**: Clears the `read_files` tracking list, preserving the security boundary between execution cycles.

### Cons
- **Token Overhead**: Structured JSON planning slightly increases LLM output token consumption compared to short, unstructured markdown lists.

## Rejected Alternatives

### 1. Unstructured Markdown Planning
We rejected generating free-text Markdown plans.
* **Why**: Unstructured markdown cannot be programmatically validated for path safety or syntactical correctness, leaving the system highly vulnerable to indirect prompt injection payloads that bypass instructions and direct the Worker to target sensitive files.

### 2. External Compiled AST Tools (Tree-sitter)
We rejected compiling or installing external binary-bound AST parsers for the planning phase.
* **Why**: To maintain the **Radical Simplicity** and pure Python execution goals of ADR-0009, we avoid introducing C-compilation, platform-specific binaries, or heavy setup dependencies (like Node.js/npm) that could fail during CI/CD or containerized deployments. High-level structure is sufficiently represented by our lightweight, stdlib-only recursive directory tree.

---

## Inspiration & References
* **Routine: A Structural Planning Framework (2025)**: Demonstrated that structured planning scripts as intermediate representations increase LLM tool-calling success rates from **41.1% to 96.3%**.
* **GitHub Issue #27**: Documented a future refactoring path for multi-language regex-based AST outlines along with the formal benchmark suite to measure trajectory length and success rate improvements.
