"""Session-wide test environment scrub.

The orchestrator CLI loads a repository-level ``.env`` at startup
(``orchestrator/__main__._load_env_file``), and some shells export
``AGENT_TRUSTED_JUDGE_USER`` for local manual runs. Either source leaks into
the test process and makes mocked judge reviews fail the trust check
(``test-judge-user`` ≠ the real login), producing host-dependent results.
Pop the trust-discriminating variables once, before any test module imports
``orchestrator.nodes``; individual test classes that need them restore their
own values in setUp/tearDown.
"""

import os

for _var in (
    "AGENT_TRUSTED_JUDGE_USER",
    "AGENT_JUDGE_CHECK_NAMES",
    "AGENT_JUDGE_ENABLED",
):
    os.environ.pop(_var, None)
