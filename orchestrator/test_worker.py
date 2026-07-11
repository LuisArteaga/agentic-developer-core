import os
import unittest
import unittest.mock

from langchain_core.messages import AIMessage
from orchestrator.worker import execute_worker, get_worker_tools


class TestWorkerAgent(unittest.TestCase):
    def setUp(self):
        # Inject mock key to isolate tests and avoid production scaffolding
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"

    def tearDown(self):
        if self.original_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self.original_api_key
        elif "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]

    def test_get_worker_tools(self):
        """Test that get_worker_tools returns the 5 expected wrapped tools."""
        wrapped_tools = get_worker_tools()
        self.assertEqual(len(wrapped_tools), 5)
        tool_names = {t.name for t in wrapped_tools}
        self.assertEqual(
            tool_names,
            {"read_file", "list_directory", "grep_search", "patch_file", "run_command"},
        )

    @unittest.mock.patch("langchain_openai.ChatOpenAI.invoke")
    def test_execute_worker_success(self, mock_invoke):
        """Test that execute_worker correctly invokes the agent and returns the final response."""
        mock_invoke.return_value = AIMessage(content="Successfully completed the task.")

        final_answer = execute_worker(
            issue_description="Verify codebase",
            plan="1. Run verify",
            node_name="execute",
        )

        self.assertEqual(final_answer, "Successfully completed the task.")
