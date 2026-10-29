"""Minimalistic coding agent with tools for reading, writing, listing, searching, and running code in a sandbox."""

import os
import subprocess
import warnings

from google.adk.agents import Agent
from google.genai import types

# The default model, shared with eval.py so there is a single source of truth.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Silencing an ADK warning to declutter terminal output
warnings.filterwarnings("ignore", message=r".*JSON_SCHEMA_FOR_FUNC_DECL.*", category=UserWarning)


def _resolve(sandbox: str, path: str) -> str:
    """Turn a sandbox-relative path into an absolute one, refusing to escape the sandbox."""
    sandbox = os.path.realpath(sandbox)
    full = os.path.realpath(os.path.join(sandbox, path))
    # Allow only the sandbox root itself or a path beneath it (blocks "../" escapes).
    if full != sandbox and not full.startswith(sandbox + os.sep):
        raise ValueError(f"path escapes sandbox: {path!r}")
    return full


def _read(sandbox: str, path: str) -> str:
    try:
        with open(_resolve(sandbox, path), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _write(sandbox: str, path: str, contents: str) -> str:
    try:
        full = _resolve(sandbox, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(contents)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Wrote {len(contents)} chars to {path}"


def _list_files(sandbox: str, directory: str = ".") -> str:
    try:
        root = _resolve(sandbox, directory)
        if not os.path.exists(root):
            return f"Error: no such directory: {directory}"
        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for fname in sorted(filenames):
                if fname.startswith(".") or fname.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fname), sandbox)
                matches.append(rel)
        return "\n".join(matches) if matches else "(empty directory)"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _run_command(sandbox: str, command: str, stdin_data: str = "") -> dict:
    try:
        sandbox_real = os.path.realpath(sandbox)
        env = {
            **os.environ,
            "PYTHONPATH": sandbox_real + os.pathsep + os.path.join(sandbox_real, "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.run(
            command,
            shell=True,
            cwd=sandbox_real,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "Error: command timed out after 15s"}
    except Exception as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"Error: {exc}"}


SYSTEM_INSTRUCTION = """You are an autonomous software engineering agent working inside a project sandbox.
You MUST use `write_file` to save all code changes (never merely print code in your final response).

Follow this strict engineering workflow on every task:

1. EXPLORE & TRACE ROOT CAUSES (Bug-Fix Tasks):
   - Always call `list_files` first to discover every file in the workspace.
   - Never assume the bug is inside the file or function named in the bug report. Before making any edit, use `read_file` to read ALL sibling modules in the package (especially upstream parsing, loading, or data-conversion helpers).
   - Trace the full data flow upstream from raw input to final output.
   - Fix the upstream root cause where data is parsed, loaded, or transformed rather than patching downstream symptoms.
   - IMPORTANT: Preserve existing Python return types (`float`, `int`, `datetime`) on library functions. Never change a function that returns `float` to return `Decimal`, because `Decimal("1.23") == 1.23` evaluates to `False` in Python assertions.

2. SYSTEMATIC EDGE-CASE & FORMAT RIGOR (Build / CLI Tasks):
   - Before writing code, enumerate boundary conditions across these categories:
     a) Sequence & range direction: If expanding a range `a-b`, support both ascending (`a <= b`, step `+1`) and descending/reversed ranges (`a > b`, step `-1`, so `9-7` expands to `9 8 7`).
     b) Float-safe half-up rounding for CLI text output: When formatting decimal/money strings for stdout in CLI programs, use `decimal.Decimal` and `.quantize(Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)` instead of binary `float` `round()`, so values like `5.005` format as `5.01` and `3.675` format as `3.68`. When summing `Decimal` values, always pass `Decimal("0")` as the start value (`sum(items, Decimal("0"))`) so empty inputs produce `Decimal("0")` instead of integer `0` (which would crash on `.quantize()`).
     c) Trailing-zero trimming: When formatting plain decimals with trailing zeros trimmed (e.g., `4.0` -> `4`, `1.50` -> `1.5`), format with fixed-point `s = f"{val:f}"`, strip trailing zeros and decimal point via `s.rstrip("0").rstrip(".") if "." in s else s`, and normalize `"-0"` to `"0"`.
     d) Empty & malformed inputs: Handle empty stdin (`""` or blank-only lines) gracefully without crashing (printing nothing, `0`, or `0.00` as required). Parse and validate all input lines (field count, valid numbers, `start <= end`) BEFORE running subcommands so malformed input always prints an error to `sys.stderr` and exits with code `2`.
     e) Exact exit codes & stderr: Unknown/missing commands, missing arguments, or malformed input must print an error message to `sys.stderr` (nothing to stdout) and exit with code `2`. Domain lookup errors (such as a missing category) must print to `sys.stderr` and exit with the specified code (e.g., `1`).

3. MANDATORY VERIFICATION LOOP:
   - Never finish without running your code via `run_command`.
   - For bug fixes: write or run a command exercising the full pipeline (both the upstream parser/loader and the downstream function) to reproduce the failure first and verify the fix afterward. Remove any temporary test files before finishing.
   - For CLI programs: use `run_command` with `stdin_data` to test all prompt examples, empty input (`stdin_data=""`), descending ranges (`9-7`), half-up rounding cases (`5.005`), malformed lines, and invalid commands, verifying `stdout`, `stderr`, and `exit_code`. Keep iterating until every check passes.
"""


def build_agent(sandbox: str, model: str) -> Agent:
    # Each tool's docstring is the description the model sees, so it states that
    # paths are sandbox-relative.
    def list_files(directory: str = ".") -> dict:
        """List all files in the sandbox (relative to the project root) to discover package structure."""
        return {"result": _list_files(sandbox, directory)}

    def read_file(path: str) -> dict:
        """Read a text file (path is relative to the project root) and return its contents."""
        return {"result": _read(sandbox, path)}

    def write_file(path: str, contents: str) -> dict:
        """Create or overwrite a text file (path is relative to the project root)."""
        return {"result": _write(sandbox, path, contents)}

    def run_command(command: str, stdin_data: str = "") -> dict:
        """Execute a shell or python command inside the sandbox with optional stdin_data, returning exit_code, stdout, and stderr."""
        return {"result": _run_command(sandbox, command, stdin_data)}

    return Agent(
        name="coding_agent",
        model=model,
        instruction=SYSTEM_INSTRUCTION,
        tools=[list_files, read_file, write_file, run_command],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW
            )
        )
    )


if __name__ == "__main__":
    # Test the agent on a prompt in ./out sandbox.  Usage: python agent.py "your prompt"
    import asyncio
    import sys
    from google.adk.runners import InMemoryRunner

    # Default to Vertex AI (like eval.py) so `python agent.py` works without a
    # Gemini API key; an existing env value, if set, takes precedence.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-04-ea7faca7f361")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    sandbox = os.path.join(os.path.dirname(os.path.realpath(__file__)), "out")
    os.makedirs(os.path.join(sandbox, "src"), exist_ok=True)
    prompt = " ".join(sys.argv[1:]) or "Write a hello world python script"

    async def _run_agent() -> None:
        runner = InMemoryRunner(agent=build_agent(sandbox, DEFAULT_MODEL))
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="me")
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(user_id="me", session_id=session.id, new_message=message):
            for part in (event.content and event.content.parts) or []:
                print(part.text or "", end="", flush=True)
    asyncio.run(_run_agent())
    import glob
    for script in glob.glob(f"{sandbox}/**/*.py", recursive=True):
        print(f"\n=== output of {os.path.relpath(script, sandbox)} ===\n" + subprocess.run([sys.executable, script], cwd=sandbox, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout, end="")
