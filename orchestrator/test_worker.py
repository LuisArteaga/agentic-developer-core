import copy
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from orchestrator import state, tools
from orchestrator.worker import execute_worker, get_chat_model, get_worker_tools

class TestWorkerAgent(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name).resolve()
        
        # Override project root for safety checks in tools
        tools._PROJECT_ROOT = self.temp_dir_path
        
        # Point AGENT_LOG_PATH to this temporary directory so state is written there
        self.original_env = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = str(self.temp_dir_path / "logs")
        
        # Inject mock key to isolate tests and avoid production scaffolding
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"
        
        # Create a clean state file
        self.state_file_path = state.get_state_filepath()
        self.test_state = copy.deepcopy(state.DEFAULT_STATE)
        state.save(self.test_state, self.state_file_path)

        # Create a test file in the temp directory
        self.test_file = self.temp_dir_path / "hello.txt"
        self.test_file.write_text("hello world", encoding="utf-8")

    def tearDown(self):
        # Restore project root and environment, and clean up
        tools._PROJECT_ROOT = None
        if self.original_env is not None:
            os.environ["AGENT_LOG_PATH"] = self.original_env
        elif "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]
            
        if self.original_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self.original_api_key
        elif "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]
            
        self.temp_dir.cleanup()

    def test_get_worker_tools(self):
        """Test that get_worker_tools returns the 5 expected wrapped tools."""
        wrapped_tools = get_worker_tools()
        self.assertEqual(len(wrapped_tools), 5)
        tool_names = {t.name for t in wrapped_tools}
        self.assertEqual(tool_names, {"read_file", "list_directory", "grep_search", "patch_file", "run_command"})

    def test_get_chat_model(self):
        """Test that get_chat_model instantiates ChatOpenAI with the correct base url."""
        model = get_chat_model("gpt-4o")
        self.assertEqual(model.model_name, "gpt-4o")
        self.assertEqual(model.openai_api_base, "https://openrouter.ai/api/v1")

    @unittest.mock.patch("langchain_openai.ChatOpenAI.invoke")
    def test_execute_worker_success(self, mock_invoke):
        """Test a successful worker execution path where it reads and patches a file."""
        
        # We define a side_effect function to simulate the LLM's multi-turn tool calling
        def llm_side_effect(input_messages, *args, **kwargs):
            # Inspect the length of messages to determine the step in the conversation
            if hasattr(input_messages, "messages"):
                msg_list = input_messages.messages
            else:
                msg_list = input_messages

            msg_count = len(msg_list)
            
            if msg_count == 2:
                # Step 1: LLM decides to read the file (SystemMessage + HumanMessage)
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "read_file",
                        "args": {"path": "hello.txt"},
                        "id": "call_read_1"
                    }]
                )
            elif msg_count == 4:
                # Step 2: LLM sees the file contents and decides to patch the file
                # [SystemMessage, HumanMessage, AIMessage, ToolMessage] -> ToolMessage is at index 3
                self.assertIsInstance(msg_list[3], ToolMessage)
                self.assertEqual(msg_list[3].content, "hello world")
                
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "patch_file",
                        "args": {
                            "path": "hello.txt",
                            "old_string": "hello",
                            "new_string": "goodbye"
                        },
                        "id": "call_patch_1"
                    }]
                )
            elif msg_count == 6:
                # Step 3: LLM sees the successful patch and returns the final answer
                self.assertIsInstance(msg_list[5], ToolMessage)
                self.assertIn("patched successfully", msg_list[5].content)
                
                return AIMessage(
                    content="I have successfully updated hello.txt from 'hello' to 'goodbye'.",
                    tool_calls=[]
                )
            else:
                self.fail(f"Unexpected message count in mock LLM: {msg_count}")

        mock_invoke.side_effect = llm_side_effect

        # Run the worker agent
        final_answer = execute_worker(
            issue_description="Please change 'hello' to 'goodbye' in hello.txt.",
            plan="1. Read hello.txt\n2. Patch hello.txt replacing 'hello' with 'goodbye'",
            model_name="gpt-4o"
        )

        # Assert final output and file changes
        self.assertEqual(final_answer, "I have successfully updated hello.txt from 'hello' to 'goodbye'.")
        self.assertEqual(self.test_file.read_text(encoding="utf-8"), "goodbye world")

        # Verify that the path was registered in state.json
        loaded_state = state.load(self.state_file_path)
        self.assertIn("hello.txt", loaded_state["read_files"])

    @unittest.mock.patch("langchain_openai.ChatOpenAI.invoke")
    def test_execute_worker_read_before_edit_violation(self, mock_invoke):
        """Test that the worker agent handles a Read-Before-Edit validation failure from the tool."""
        
        def llm_side_effect(input_messages, *args, **kwargs):
            if hasattr(input_messages, "messages"):
                msg_list = input_messages.messages
            else:
                msg_list = input_messages

            msg_count = len(msg_list)
            
            if msg_count == 2:
                # Step 1: LLM immediately tries to patch without reading first
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "patch_file",
                        "args": {
                            "path": "hello.txt",
                            "old_string": "hello",
                            "new_string": "goodbye"
                        },
                        "id": "call_patch_violating"
                    }]
                )
            elif msg_count == 4:
                # Step 2: LLM receives the validation error from the tool
                self.assertIsInstance(msg_list[3], ToolMessage)
                self.assertIn("Read-Before-Edit validation failed", msg_list[3].content)
                
                return AIMessage(
                    content="I failed to edit the file because I violated the Read-Before-Edit rule.",
                    tool_calls=[]
                )
            else:
                self.fail(f"Unexpected message count: {msg_count}")

        mock_invoke.side_effect = llm_side_effect

        final_answer = execute_worker(
            issue_description="Please change 'hello' to 'goodbye' in hello.txt.",
            plan="1. Patch hello.txt",
            model_name="gpt-4o"
        )

        self.assertEqual(final_answer, "I failed to edit the file because I violated the Read-Before-Edit rule.")
        # Ensure the file was NOT modified
        self.assertEqual(self.test_file.read_text(encoding="utf-8"), "hello world")

    @unittest.mock.patch("langchain_openai.ChatOpenAI.invoke")
    def test_execute_worker_recursion_limit(self, mock_invoke):
        """Test that the worker agent aborts when the recursion limit is hit during an infinite loop."""
        
        call_count = 0
        def mock_recursion_invoke(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "list_directory",
                    "args": {"path": "."},
                    "id": f"call_loop_{call_count}"
                }]
            )
        
        mock_invoke.side_effect = mock_recursion_invoke

        # LangGraph's prebuilt create_react_agent handles recursion limit exhaustion
        # by returning a fallback AIMessage instead of raising GraphRecursionError
        final_answer = execute_worker(
            issue_description="List the directory infinitely.",
            plan="Loop forever.",
            model_name="gpt-4o"
        )
        
        self.assertEqual(final_answer, "Sorry, need more steps to process this request.")

    def test_run_command_security_validation(self):
        """Test that the command security validation correctly allows safe commands and blocks unsafe ones."""
        
        # 1. Allowed commands
        res = get_worker_tools()[4].invoke("make verify")
        # Should not be blocked by safety check (may fail if make is not configured, but won't return security validation error)
        self.assertNotIn("Security validation failed", res)
        
        res = get_worker_tools()[4].invoke("python3 -m unittest discover -s . -p 'test_*.py'")
        self.assertNotIn("Security validation failed", res)

        res = get_worker_tools()[4].invoke("pytest")
        self.assertNotIn("Security validation failed", res)
        
        res = get_worker_tools()[4].invoke("uv pip list")
        self.assertNotIn("Security validation failed", res)
        
        # 2. Blocked executables
        res = get_worker_tools()[4].invoke("rm -rf /")
        self.assertIn("Security validation failed: Executable 'rm' is not in the permitted allowlist", res)
        
        res = get_worker_tools()[4].invoke("curl http://example.com")
        self.assertIn("Security validation failed: Executable 'curl' is not in the permitted allowlist", res)
        
        # 3. Blocked inline Python scripts (-c flag)
        res = get_worker_tools()[4].invoke("python3 -c \"print(1)\"")
        self.assertIn("Security validation failed: Executing arbitrary inline Python scripts via the '-c' flag is blocked", res)
        
        res = get_worker_tools()[4].invoke("python -c 'print(1)'")
        self.assertIn("Security validation failed: Executing arbitrary inline Python scripts via the '-c' flag is blocked", res)

        # 4. Blocked shell-injection and control characters
        res = get_worker_tools()[4].invoke("make verify ; rm -rf /")
        self.assertIn("Security validation failed: Command contains blocked character ';'", res)
        
        res = get_worker_tools()[4].invoke("make verify | grep test")
        self.assertIn("Security validation failed: Command contains blocked character '|'", res)
        
        res = get_worker_tools()[4].invoke("make verify && pytest")
        self.assertIn("Security validation failed: Command contains blocked character '&'", res)
        
        res = get_worker_tools()[4].invoke("make verify\nrm -rf /")
        self.assertIn("Security validation failed: Command contains blocked character '\\n'", res)

        # 5. Blocked uv subcommands (preventing RCE proxying)
        res = get_worker_tools()[4].invoke("uv run rm -rf /")
        self.assertIn("Security validation failed: 'uv' subcommand 'run' is blocked", res)
        
        res = get_worker_tools()[4].invoke("uv tool run pytest")
        self.assertIn("Security validation failed: 'uv' subcommand 'tool' is blocked", res)
        
        res = get_worker_tools()[4].invoke("uv")
        self.assertIn("Security validation failed: 'uv' command requires a subcommand", res)
