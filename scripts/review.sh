#!/bin/sh
# AGENT_RESOLVED: scripts/review.sh is now a wrapper around scripts/review.py to support OpenTelemetry tracing.
set -u
python3 scripts/review.py "$@"
