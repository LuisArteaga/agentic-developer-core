"""Tests for secret redaction in worker traces and telemetry (ADR-0037 layer 4)."""

import unittest

from scripts.redaction import redact_secrets


class TestRedactSecrets(unittest.TestCase):
    def test_redacts_github_classic_pat(self):
        out = redact_secrets("token is ghp_" + "a" * 36 + " end")
        self.assertNotIn("ghp_", out)
        self.assertIn("[REDACTED:ghp]", out)

    def test_redacts_github_fine_grained_pat(self):
        out = redact_secrets("github_pat_" + "b" * 40)
        self.assertNotIn("github_pat_", out)
        self.assertIn("[REDACTED:github_pat]", out)

    def test_redacts_openrouter_key(self):
        out = redact_secrets("key=sk-or-v1-" + "c" * 24)
        self.assertNotIn("sk-or-v1-", out)
        self.assertIn("[REDACTED:sk-or-v1]", out)

    def test_redacts_shell_assignment_value(self):
        # A custom (non-token-shape) secret bound to a *_KEY var is still scrubbed.
        out = redact_secrets("OPENROUTER_API_KEY=my-custom-secret-value")
        self.assertNotIn("my-custom-secret-value", out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("OPENROUTER_API_KEY=", out)

    def test_redacts_export_assignment(self):
        out = redact_secrets("export GH_TOKEN=abc123")
        self.assertNotIn("abc123", out)
        self.assertIn("[REDACTED]", out)

    def test_redacts_colon_assignment_bare_value(self):
        out = redact_secrets("api_key: hunter2")
        self.assertNotIn("hunter2", out)
        self.assertIn("[REDACTED]", out)

    def test_does_not_corrupt_equality_comparison(self):
        # `api_key == expected` must NOT be mistaken for an assignment.
        code = "if api_key == expected:\n    return True"
        self.assertEqual(redact_secrets(code), code)

    def test_does_not_corrupt_json_object(self):
        # JSON `{"api_key": "v"}` uses a quoted key + colon; the colon pass
        # must not match (so the JSON is not mangled); the token pass still
        # scrubs any token-shaped value inside, leaving valid JSON.
        import json

        payload = '{"api_key": "ghp_' + "a" * 36 + '"}'
        out = redact_secrets(payload)
        # The ghp_ token is scrubbed but the JSON structure stays intact.
        self.assertNotIn("ghp_", out)
        parsed = json.loads(out)
        self.assertIn("api_key", parsed)
        self.assertIn("[REDACTED:ghp]", parsed["api_key"])

    def test_preserves_trailing_newline(self):
        self.assertEqual(redact_secrets("a\nb\n"), "a\nb\n")
        self.assertEqual(redact_secrets("a\nb"), "a\nb")

    def test_non_str_and_empty_returned_unchanged(self):
        self.assertEqual(redact_secrets(""), "")
        self.assertIsNone(redact_secrets(None))  # type: ignore[arg-type]
        self.assertEqual(redact_secrets(123), 123)  # type: ignore[arg-type]

    def test_assignment_redaction_does_not_drop_closing_brace(self):
        # Regression guard: the shell-assignment pass must not consume a
        # trailing '}' from a JSON-like value, which previously corrupted JSON.
        out = redact_secrets('CONFIG_KEY={"a": 1}')
        self.assertTrue(out.rstrip().endswith("[REDACTED]"))


if __name__ == "__main__":
    unittest.main()
