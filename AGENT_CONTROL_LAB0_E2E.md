# Agent Control end-to-end on Splunk O11y Cloud (lab0)

`agent_control_lab0_e2e.py` is a QE reproduction script. In one run it proves:

1. A `@control`-decorated tool call is evaluated server-side through the O11y gateway.
2. A bound control (regex "2FA for transfers of $10,000 or more") fires and STEERS (blocks) the call.
3. The control execution shows up as a span inside the same trace in the AO UI, tagged
   "Triggered", with the tool span and the matched control spans under one session.

## Background

- O11y Cloud supports the splunk-ao SDK only. The plain galileo SDK is not supported here.
  `agent_control` (enforcement) is a separate SDK and works through the gateway.
- The `agent_control` feature flag must be ON for the cluster (resolved per cluster
  customer_name, e.g. `o11y-lab0`, not per org).

## Install

```
pip install "agent-control-sdk==8.5.0" "splunk-ao"
```

Install them as two separate packages. There is no `agent-control-sdk[splunk-ao]` extra.

## Prereqs

- VPN: GlobalProtect US West Full Tunnel plus Aviatrix.
- Org membership: you must be provisioned into the AO org (gateway auth is pre-provisioned only).
- A control bound to your agent stream. The regex control used here matches tool input against
  `[1-9][0-9]{4,}` (any integer >= 10000) with decision `steer`.

## Environment

Set these before running. Do not commit real token values.

```
export SPLUNK_AO_REALM="lab0"
export SPLUNK_AO_O11Y_TOKEN="<ingest-token>"       # OTLP span export
export SPLUNK_AO_O11Y_API_TOKEN="<api-token>"      # CRUD (project/stream lookup)
export AC_SF_TOKEN="<session-sf-token>"            # gateway auth, sent as X-SF-Token
export AC_PROJECT_ID="<project-id>"
export AC_STREAM_ID="<agent-stream-id>"
export AC_GATEWAY="https://app.<realm>.signalfx.com/ao/agent-control"
export AC_AGENT_NAME="<agent-name>"
export AC_AMOUNT="75000"                           # any integer >= 10000 matches the control
```

These are three different token scopes, do not mix them up:
- SF/session token: gateway auth (agent-control API, control CRUD, evaluation).
- INGEST token: splunk-ao OTLP span export. Only works on the ingest endpoint.
- API token: splunk-ao CRUD. Needed because the SDK resolves project/stream by name before
  sending spans.

## Run

```
python agent_control_lab0_e2e.py
```

Expected output: `STEER FIRED ...` and `INGESTED trace_id=... steered=True`.

Then open the AO UI Tracing tab for the stream, set the range to Last 15 minutes, and open the
newest session. The trace tree shows the control node with the tool span and two control spans
under it. The control span detail shows Controls 1 / Triggered with output
`{"action":"steer","matched":true,"confidence":1}`.

## The four things that make the control render in the UI trace tree

Miss any one of these and the session shows Traces 0:

1. `observability_sink_name="registered"` on `agent_control.init`, so control events go to the
   splunk-ao bridge sink (`add_control_span`) instead of agent_control's own event store.
2. `func.tool_name` set before `@control()`, so the step is a tool (not llm). An llm step pulls
   in the Luna control, which errors when the Luna SLM backend is unavailable.
3. `set_trace_context_provider(...)` with the splunk-ao trace id, so the control span shares the
   telemetry trace id. Get it from `logger.current_parent().id` after `start_trace`;
   `splunk_ao_context.get_current_trace()` returns None and does not work as the source.
4. `await agent_control.shutdown_observability()` before exit, so the background event batcher
   flushes. Without it the events are dropped on interpreter shutdown.

Also: do not use a named `start_session`. Let splunk-ao own the session and trace.
