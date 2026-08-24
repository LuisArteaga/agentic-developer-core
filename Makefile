.PHONY: verify setup secret-scan lint format-check type-check test pre-commit-strict diff-coverage

verify: lint format-check type-check test

setup:
	@echo "Installing pre-commit git hooks..."
	pre-commit install
	@echo "Git hooks installed successfully."

secret-scan:
	python3 scripts/secret_scan.py

test:
	python -m pytest

lint:
	python -m ruff check orchestrator scripts

format-check:
	python -m ruff format --check orchestrator scripts

type-check:
	python -m mypy orchestrator scripts

# Opt-in strict security gate: the deterministic fast gates (ruff/mypy) plus
# semgrep, mirroring the CI security scan so security-sensitive changes can be
# caught at commit time rather than minutes later in CI. Does NOT run tests
# (slow) or pip-audit (network-bound CI concern) — see ADR-0020 for the
# tiered quality-gate model. `--error` is required: semgrep exits 0 on
# findings by default, so without it this would advise but not block.
pre-commit-strict: lint format-check type-check
	semgrep scan --config=auto --error orchestrator scripts

# Opt-in diff coverage gate: fails when any production line added or modified
# by this branch's diff is not exercised by the test suite — the arithmetic
# the Test Coverage PR Review Judge performs under Q4, moved out of the LLM
# into a deterministic script. 100% changed-line threshold, stateless; does
# NOT replace the global --cov-fail-under percentage (see ADR-0052). Not part
# of `verify` (full-suite runtime cost) and not wired into pre-commit, per the
# tiered-gate model of ADR-0020.
diff-coverage:
	python -m pytest --cov=orchestrator --cov=scripts --cov-report=term-missing --cov-report=json:coverage.json && python3 scripts/diff_coverage_gate.py --coverage-json coverage.json --base main
