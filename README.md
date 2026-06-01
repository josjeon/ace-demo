# Agent Control Galileo E2E

Standalone demo app for exercising real Agent Control `ControlExecutionEvent`s through the Galileo Python bridge and into Galileo as `ControlSpan`s, while the Agent Control server itself runs remotely.

For a step-by-step Forward Deploy Engineer runbook, including manual Console setup, devstack routes, commands, and troubleshooting, see [`FDE_RUNBOOK.md`](FDE_RUNBOOK.md).

## What This Demo Proves

- Agent Control is installed and running as a remote service in the Galileo cluster.
- `GalileoLogger` auto-registers the Agent Control sink/provider when the Agent Control SDK is importable.
- Real `@control()` decorator calls emit `ControlExecutionEvent`s for server-side controls.
- Evaluation uses the runtime JWT flow bound to the target Galileo log stream.
- The remote `galileo.luna` evaluator invokes Galileo's `/scorers/invoke` API directly.
- The active Galileo workflow provides trace/span context to Agent Control through the current `GalileoLogger` bridge.
- Those events flow through Agent Control's registered sink path into the Galileo bridge.
- The bridge converts them into Galileo `ControlSpan`s during the same agent run.
- The stored control spans can be queried back through the trends dashboard and custom metrics APIs.

## Local Python Environment

The demo does not require local `agent-control`, `orbit`, or `galileo-python` checkouts. Agent Control runs as a remote service, Orbit is already part of the stack, and this package installs `galileo[openai]` plus the Agent Control SDK/evaluators from PyPI.

```bash
cd ~/code/agent-control-galileo-e2e
python3.12 -m venv .venv
export DEMO_PYTHON="$PWD/.venv/bin/python"
"$DEMO_PYTHON" -m pip install --upgrade pip
"$DEMO_PYTHON" -m pip install .
```

## Pre-requisites:

Recommended env vars:

```bash
export GALILEO_CONSOLE_URL='https://console-test-evals.gcp-dev.galileo.ai'
export GALILEO_API_URL='https://api-test-evals.gcp-dev.galileo.ai'
export GALILEO_API_KEY='<your-api-key>'
export GALILEO_PROJECT='test-evals-project'
export GALILEO_LOG_STREAM='test-evals-logstream'
export AGENT_CONTROL_URL='https://agent-control-test-evals.gcp-dev.galileo.ai'
export AGENT_CONTROL_AGENT_NAME='galileo-control-span-demo'
export AGENT_CONTROL_TARGET_TYPE='log_stream'
export AGENT_CONTROL_RUNTIME_AUTH_MODE='jwt'
```

For Agent Control Enterprise, use the Galileo API key with the `Galileo-API-Key` header. If `AGENT_CONTROL_API_KEY` is also set, keep it equal to `GALILEO_API_KEY`; stale OSS keys can cause `401 Unauthorized` during Agent Control init.

## 1. Banking Transfer Controls

The demo expects these controls to be created in Console and bound to the Galileo log stream. Use [controls_for_ui.json](/Users/namrataghadi/code/agent-control-galileo-e2e/controls_for_ui.json:1) as the raw field reference when creating the controls manually in the UI. Pass `--setup-controls` only as a less-preferred fallback when UI setup is blocked.

- `demo-observe-luna-transfer-request`: denies prompt-injection attempts in the pre-LLM banking request with the Galileo Luna `prompt_injection_luna` scorer.
- `demo-steer-large-transfer-2fa`: steers transfers of `$10,000` or more by returning retry flags that set `verified_2fa=true`.

Create the controls at the log stream level in Console. The app uses Agent Control's `@control()` decorator, so control matching sees the decorated function names as step names:

- LLM function: `draft_transfer_plan`
- Tool function: `process_wire_transfer`

Do the step-name filtering in the control UI, for example with regexes that match those function names. The demo does not pass `steps`, `llm_step_name`, or `tool_step_name` through `agent_control.init(...)`.

`run_demo.py` registers the demo agent with the resolved log stream target, then verifies the effective target controls through Agent Control before evaluating. It does not create or update controls unless `--setup-controls` is passed explicitly.

## 2. Run The End-To-End Demo

```bash
cd /Users/namrataghadi/code/agent-control-galileo-e2e
"$DEMO_PYTHON" run_demo.py \
  --verify-api \
  --query-trends
```

This uses `batch` mode by default and now performs the full flow:

- registers the demo agent against the resolved Galileo log stream target
- verifies the existing log-stream-bound controls with `GET /api/v1/agents/{agent}/controls`
- verifies Agent Control runtime JWT exchange for the Galileo log stream target
- verifies Galileo `/scorers/invoke` without sending `project_id` before the Agent Control Luna evaluator path uses it
- emits real control execution events through the Galileo logger bridge
- verifies the stored trace and control spans through the public APIs
- creates or fetches the trends dashboard for the target log stream
- runs multiple `/projects/{project_id}/metrics/custom_search` chart queries with different control-focused filters

Default demo inputs are chosen to trigger 2FA steering and then complete the transfer:

- prompt: `Wire $15,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-014.`
- parsed transfer: amount `$15,000`, recipient `Horizon Robotics`, destination `United Kingdom`
- first tool pre-check steers because 2FA is missing
- the demo applies the steering context, retries with `verified_2fa=true`, and then executes the transfer

The runner prints:

- the control IDs attached to the demo agent
- whether the Galileo bridge auto-registered
- how many Agent Control sinks are registered
- the Agent Control server URL, runtime auth mode, and target binding
- runtime JWT exchange metadata, without printing the token
- `/scorers/invoke` response metadata, including `project_id=not-sent`
- the evaluation results returned by Agent Control
- the Galileo project, log stream, session, and trace IDs
- the Galileo trace ID
- the control spans attached during the run
- devstack verification results for:
  - `GET /current_user`
  - `GET /ingest/healthz`
  - `GET /projects/{project_id}/traces/{trace_id}`
  - `GET /projects/{project_id}/sessions/{session_id}`
  - `POST /projects/{project_id}/spans/partial_search`
  - `GET /projects/{project_id}/log_streams/{log_stream_id}/trends`
  - `POST /projects/{project_id}/metrics/custom_search` for several control-oriented chart slices

## 3. Optional Variants

Batch mode:

```bash
"$DEMO_PYTHON" run_demo.py \
  --mode batch
```

Distributed mode:

```bash
"$DEMO_PYTHON" run_demo.py \
  --mode distributed \
  --verify-api \
  --query-trends
```

Custom inputs:

```bash
"$DEMO_PYTHON" run_demo.py \
  --prompt "Wire $75,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-099." \
  --project my-project \
  --log-stream my-log-stream \
  --setup-controls \
  --verify-api \
  --query-trends
```

Force a hard deny:

```bash
"$DEMO_PYTHON" run_demo.py \
  --prompt "Wire $5,000 to Research Bureau in Iran for consulting." \
  --setup-controls
```

## 4. Interactive Streamlit App

`banking_streamlit_app.py` is an interactive version of the same standalone banking demo. It does not use Strands or hooks. It uses the same `@control()` decorator path as `run_demo.py`, displays each evaluation stage, shows steering retries, and prints the Galileo trace metadata created by the run.

The app uses the same decorated functions as the CLI run, so keep the log-stream-level controls in Console aligned with `draft_transfer_plan` and `process_wire_transfer`.

Streamlit is installed by default when you run `$DEMO_PYTHON -m pip install .`.

Run the app:

```bash
cd /Users/namrataghadi/code/agent-control-galileo-e2e
"$DEMO_PYTHON" -m streamlit run banking_streamlit_app.py
```

The app includes scenarios for:

- 2FA steering
- Luna prompt-injection deny

## Notes

- This demo treats control spans as independent Galileo spans attached under the active workflow for the run.
- The demo uses real Agent Control evaluation events. App code does not call `logger.add_control_span(...)` directly.
- The demo mirrors the earlier devstack retest flow by validating the public ingest health endpoint and fetching the persisted trace/span records back from the public API.
- Batch mode is verified on this stack. In my retest, distributed mode attached the spans locally but the public trace/span read APIs returned no spans at all for that run, which points to a separate streaming-ingest/readback issue.
