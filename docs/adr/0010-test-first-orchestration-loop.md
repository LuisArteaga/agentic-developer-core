# ADR 0010: Test-First Orchestration Loop with Test-Writer Node

## Status
Proposed

## Context
In our current orchestrator pipeline design ([PRD.md](./../../PRD.md) §1 & [ADR-0009](./0009-pure-python-langgraph-orchestrator.md)), the workflow transitions from planning straight to execution (Worker / Execute Node). The Worker is responsible for implementing code changes and verifying them during the subsequent Verify Node.

However, recent web research and agent evaluations indicate that Large Language Models (LLMs) produce significantly higher quality, more correct, and more reliable code when integrated into a Test-Driven Development (TDD) or "Test-First" workflow. 
Writing tests first provides:
1. **Concrete Functional Specifications**: The tests specify exactly what target classes and functions should behave like, reducing ambiguity.
2. **Import/Syntax Verification**: Compiling and running tests against stub files verifies the interfaces before implementation begins.
3. **Strong Feedback Loops**: The Worker has a concrete target (making the newly generated tests pass) with clear feedback from failing test assertions.

We need to adapt our LangGraph state machine structure to enforce this Test-First approach.

## Decision
We will introduce a new high-level node, **Test-Writer Node**, into the LangGraph macro lifecycle. 

The modified flow will compile as follows:
`Claim -> Plan -> Test-Writer -> Execute (Worker) -> Verify -> PR -> Merge`

### 1. Test-Writer Node Responsibilities
The Test-Writer Node executes after the `Plan` node. Its responsibilities are:
- **Write Sophisticated Unit Tests**: Generate comprehensive unit tests (e.g. using standard library `unittest` or `pytest`) under the appropriate test directory, covering the success paths, failure paths, and edge cases described in the plan.
- **Generate Skeleton/Stub Files**: Generate minimal stub files for any new classes, functions, or modules that do not exist yet in the codebase. The stubs will contain only the signatures and empty blocks (e.g., `pass` or `raise NotImplementedError`) so that they can be successfully imported by the test suite.
- **Pre-Verification Check**: Perform a quick syntax and import validation check by running the newly generated tests using the Python interpreter. The run is expected to fail on test assertions (which is correct for TDD) but must not crash due to syntax errors or `ModuleNotFoundError`/`ImportError`. If pre-verification fails, the node retries test generation.

### 2. Worker (Execute Node) Responsibilities
Once the tests are pre-verified, the loop transitions to the `Execute` node.
- The Worker reads the newly generated tests and stubs.
- The Worker modifies the stub files to implement the actual business logic, iteratively running the test suite until all tests pass.

## Consequences

### Pros
- **Higher Success Rates**: The code generator has a clear, executable specification to target, leading to much higher code correctness.
- **Reduced State Drift**: Stubs prevent syntax crashes while tests verify the interface contracts early.
- **Fewer PR Regressions**: Verifying against new tests before PR creation guarantees the requested feature works as expected.

### Cons
- **Higher Token Consumption**: Writing both tests and code in separate nodes increases the overall token usage of the loop.
- **Self-Testing Risk**: If the Test-Writer node generates faulty test assumptions, the Worker will implement code that satisfies those incorrect assumptions. Human-in-the-loop review of the PR and tests remains necessary.

## Rejected Alternatives

### 1. Code-First with Parallel Test Generation
We rejected having the Worker write both code and tests together in the same node.
- **Why**: When the LLM is tasked with writing both simultaneously, it often simplifies the tests to match its own lazy code implementation, missing edge cases and bypassing verification contracts. Separating test writing into a prior step guarantees that the interface is designed before implementation details are considered.

### 2. Dynamic Import Mocking
We rejected having the Test-Writer mock all imports rather than writing stub files.
- **Why**: Mocking imports is complex, fragile, and doesn't provide the Worker with target files to edit. Writing minimal stub files creates the exact physical file structure in the workspace, making it easy for the Worker to locate and implement the code.
