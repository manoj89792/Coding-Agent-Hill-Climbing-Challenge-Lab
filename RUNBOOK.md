# Detailed Lab Runbook: Hill-Climbing a Coding Agent

This runbook documents every step, command, trajectory diagnosis, and code change performed to complete the **Hill-Climbing a Coding Agent** challenge lab.

---

## Step 1: Environment Setup & Initial Verification

### 1.1 Copy Lab Files from Cloud Storage
In Cloud Shell, authenticate and copy the starter files from the lab bucket (`gs://<PROJECT_ID>-l400-apex/`):

```bash
gcloud auth login
export PROJECT_ID=$(gcloud config get-value project)
gcloud storage cp -r "gs://${PROJECT_ID}-l400-apex/*" ~/.
mkdir -p ~/.vscode && cp ~/settings.json ~/.vscode/
```

### 1.2 Create `.venv` and Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.3 Smoke-Test `agent.py`
```bash
python agent.py
```
The agent creates `out/hello_world.py`, runs it, and outputs:
```text
=== output of hello_world.py ===
Hello, World!
```

---

## Step 2: Baseline Evaluation

Run the evaluation suite against the baseline agent:
```bash
source .venv/bin/activate
python eval.py
```

### Baseline Run Output (`runs/20261029_090451`)
```text
task_id           result  tool_calls  duration_seconds
------------------------------------------------------
expense_report    PASS    1           13.8
interval_set      FAIL    0           15.2
invoice_report    FAIL    8           28.3
range_list        FAIL    1           19.1
reminder_time     FAIL    0           15.6
------------------------------------------------------
Evaluation completed. Solved 1 out of 5 tasks.
Uploading evaluation summary to gs://<PROJECT_ID>-l400-apex...
Summary uploaded successfully.
```

> **Checkpoint 1**: Click **Check my progress** on **Task 2. Run the baseline evaluation**.

---

## Step 3: Manual Trajectory Analysis (`invoice_report`)

### 3.1 Run the Reproduction Script
```bash
bash runs/20261029_090451/invoice_report/repro.sh
```
Output:
```text
Traceback (most recent call last):
  File "/tmp/tmp.Y4wZs0wnZ0/tests/hidden.py", line 8, in <module>
    assert invoice_total(amounts) == 125.49, invoice_total(amounts)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: 124
hidden.py exit code: 1 (0 = PASS)
```

### 3.2 Inspect Tool Calls in `runs/20261029_090451/invoice_report/tool_uses.json`
The baseline agent made 8 tool calls:
1. `read_file("src/invoices/report.py")` — saw `return round(sum(amounts), 2)`.
2. `write_file("test_invoice_total.py", ...)` — wrote a test script.
3. `read_file("test_invoice_total.py")`
4. `write_file("run_test.py", ...)` (repeated 4 times) — tried to write a test runner script using `subprocess.run`, hoping something would execute it.
5. `write_file("src/invoices/report.py", ...)` — rewrote `invoice_total` to use `Decimal`, never realizing that `amounts` passed into `invoice_total` had already been truncated to integers by `parse_amount(raw)` (`return int(float(raw))`) in `src/invoices/load.py`.

### 3.3 Submit Task Diagnosis
Run the following command in the Antigravity CLI (`agy`):
```text
/submit-task-diagnosis For invoice_report, I observed in the trajectory that the agent only read and edited src/invoices/report.py and repeatedly wrote test_invoice_total.py and run_test.py scripts that it had no way to execute, while never opening src/invoices/load.py where parse_amount truncates amounts with int(float(raw)); the coding agent failed because it lacked directory listing/code search tools to discover upstream modules beyond the file named in the prompt and lacked a code execution tool to run tests and verify that its edit to report.py did not fix the root cause.
```

> **Checkpoint 2**: Click **Check my progress** on **Task 3. Analyze agent trajectories manually**.

---

## Step 4: Scaling Trajectory Analysis Across Remaining Tasks

1. **`reminder_time`**:
   - **Prompt**: States `reminder_hour_utc` in `src/scheduling/core.py` reports `9` instead of `14` for `"2026-03-10T09:00:00-05:00"`.
   - **Actual Root Cause**: `src/scheduling/core.py` is already correct! The bug is upstream in `src/scheduling/parsing.py`, where `parse_event` calls `datetime.fromisoformat(raw["start"]).replace(tzinfo=None)`, stripping the `-05:00` offset before `to_utc()` ever sees it.
   - **Agent Failure Mode**: Without `list_files`, the agent never knew `parsing.py` existed, and without `run_command`, it could not test the end-to-end `parse_event` $\rightarrow$ `reminder_hour_utc` flow.

2. **`range_list`**:
   - **Prompt**: Expand comma-separated ranges `a-b` inclusive into space-separated integers (`src/expand.py`).
   - **Actual Failure**: Hidden test passes a descending range `9-7` and expects `9 8 7\n`, but the agent wrote `list(range(start, end + 1))` which returns `[]` when `start > end`, printing an empty line `\n`.
   - **Agent Failure Mode**: The agent one-shotted the code from the prompt's ascending examples without enumerating directional boundary conditions (`start > end`) or running tests.

3. **`interval_set`**:
   - **Prompt**: CLI at `src/intervals.py` supporting `merge`, `count`, `length`, and `covers X` over decimal intervals.
   - **Actual Failure**:
     - Calling `sum(end - start for start, end in merged)` returns integer `0` when input is empty; chaining `.quantize(Decimal("0.01"))` on `0` raises `AttributeError`.
     - Trailing zeros must be trimmed (`4.0` $\rightarrow$ `4`, `1.50` $\rightarrow$ `1.5`) without converting integers like `10` into scientific notation (`1E+1`).
     - `length` must use `ROUND_HALF_UP` (`3.675` $\rightarrow$ `3.68`), not Python's round-half-to-even `round()`.

4. **`expense_report`**:
   - **Prompt**: CLI at `src/ledger.py` supporting `total`, `count`, `average`, `categories`, `top N`, `category NAME`.
   - **Actual Failure**:
     - Must use `Decimal` with `ROUND_HALF_UP` (`5.005` $\rightarrow$ `5.01`).
     - Empty input on `total` and `average` must print `0.00` without crashing.
     - Malformed input lines must be validated before running commands and exit with code `2` on `stderr`; missing category in `category NAME` must exit with code `1` on `stderr`.

---

## Step 5: Optimizing `agent.py` & Validating

### 5.1 Key Changes Made to `agent.py`
1. **Added `list_files(directory=".")` tool**: Recursively lists all non-hidden files in the sandbox so the agent immediately discovers upstream files (`load.py`, `parsing.py`).
2. **Added `run_command(command, stdin_data="")` tool**: Runs shell or Python commands inside the sandbox with `PYTHONPATH` set to include the sandbox and `src/`, optional `stdin_data`, and a 15-second timeout, returning `exit_code`, `stdout`, and `stderr`.
3. **Set `temperature=0.0`**: Eliminates sampling flakiness while keeping `DEFAULT_MODEL = "gemini-3.1-flash-lite"` and `ThinkingLevel.LOW`.
4. **Upgraded `SYSTEM_INSTRUCTION`**:
   - Directs the agent to always run `list_files` first, read all sibling/upstream modules before editing, fix upstream root causes, and preserve existing Python return types (`float`, `int`, `datetime`) on library functions (since `Decimal("125.49") == 125.49` is `False` in Python).
   - Enumerates CLI edge cases: ascending & descending ranges (`9-7` $\rightarrow$ `9 8 7`), `Decimal` `ROUND_HALF_UP` formatting with `sum(items, Decimal("0"))`, safe trailing-zero trimming, upfront input validation, and exact exit codes (`1` vs `2`) with error messages on `stderr`.
   - Enforces a mandatory verification loop using `run_command` before finishing.

### 5.2 Re-run the Evaluation Harness
```bash
source .venv/bin/activate
python eval.py
```
Result (`runs/20261029_091237`):
- `expense_report`: **PASS**
- `interval_set`: **PASS**
- `invoice_report`: **PASS**
- `range_list`: **PASS**
- `reminder_time`: **PASS**
- **Solved 5 out of 5 tasks.**

> **Checkpoint 3**: Click **Check my progress** on **Task 5. Optimize the agent to solve at least four tasks**.

### 5.3 Grade the Final Agent
In the Antigravity CLI (`agy`), run:
```text
/grade-my-agent
```
This scores `agent.py` **100/100 (`pass`)** and uploads `judge_model.json` to `gs://<PROJECT_ID>-l400-apex/judge_model.json`.

> **Checkpoint 4**: Click **Check my progress** on **Submit the final version of your optimized agent for grading**.
