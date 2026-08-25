# Agent Control on Splunk O11y Cloud (lab0): architecture and step-by-step test guide

This walks through how O11y, Agent Control, and the SDKs fit together, how to turn on the
feature flag, how to set up a project / agent stream / control in the AO UI, and how to run
the end-to-end test. Every step is drawn out so QE can follow it top to bottom.

Companion files: `agent_control_lab0_e2e.py` (the reproduction script) and
`AGENT_CONTROL_LAB0_E2E.md` (the short run guide).

---

## 1. How it fits together (component map)

```
   Your agent app (Python)
        |
        |  uses TWO separate SDKs (installed as two packages)
        |
   +----+-----------------------------+
   |                                  |
   v                                  v
 agent_control SDK                splunk-ao SDK (SAO SDK)
 (enforcement)                    (telemetry / logging)
 - @control decorator             - sends spans over OTLP
 - runtime-token exchange         - built on galileo-core
 - evaluate / steer / deny        - the ONLY supported O11y Cloud telemetry SDK
        |                                  |
        |  both go through the O11y gateway |
        +----------------+-----------------+
                         |
                         v
        +-------------------------------------------+
        |  O11y API Gateway  (app.<realm>.signalfx) |
        |  - authenticates X-SF-Token               |
        |  - puts its own identity JWT on           |
        |    Authorization                          |
        |  - strips /ao/agent-control and /ao/api   |
        |  - passes X-Agent-Control-Runtime-Token   |
        |    through untouched (Option A)           |
        +-------------------------------------------+
              |                          |
              v                          v
   Agent Control server          Galileo api service
   (o11y-ao namespace)           (evaluates flags, CRUD,
   image v0.2.74 = 8.5.0         serves /ao/api/configuration)
   - reads runtime token from
     X-Agent-Control-Runtime-Token
   - runs the control, returns
     steer / deny / allow
              |
              v
   OTLP ingest (ingest.<realm>.observability.splunkcloud.com)
   spans land here and show in the AO UI Tracing tab
```

Key idea (Option A): the gateway owns the `Authorization` header for its identity JWT, so the
Agent Control runtime token rides a separate header, `X-Agent-Control-Runtime-Token`. The two
never collide.

Which SDK where:

```
  O11y Cloud (SaaS, lab0/rc0):  splunk-ao SDK only   (galileo SDK NOT supported)
  OnPrem:                       galileo SDK (to be discontinued) + splunk-ao
  agent_control SDK:            separate, enforcement, works via the gateway in both
```

---

## 2. Turn on the feature flag (agent_control)

The AO UI hides Agent Control until the `agent_control` feature flag is ON for the cluster.

```
   feature-flags.json (orbit repo)
     defaults:      { ... no agent_control ... }
     o11y-lab0:     { ... }      <- lab0 reads THIS block (per cluster customer_name)
        |
        |  resolved by the api service as:  defaults | customer_override | env-var
        |  (env-var wins)
        v
   GET /ao/api/configuration   ->   feature_flags.agent_control : true/false
```

The flag is per cluster (customer_name `o11y-lab0`), not per org. Turning it on enables it for
the whole lab0 cluster.

Temporary enable (env override on the api deployment):

```
   Step A: kubectl config use-context lab0
   Step B: kubectl -n o11y-ao set env deployment/api \
             GALILEO_FEATURE_FLAG_AGENT_CONTROL=enabled
   Step C: wait ~60s (flag cache TTL) for the api pods to roll
   Step D: verify
           curl -s -H "X-SF-Token: <sf-token>" \
             https://app.lab0.signalfx.com/ao/api/configuration
           -> feature_flags.agent_control : true
   Rollback: kubectl -n o11y-ao set env deployment/api GALILEO_FEATURE_FLAG_AGENT_CONTROL-
```

Permanent (preferred): orbit PR #1870 adds `"agent_control": "enabled"` to the `o11y-lab0`
block in `configs/feature-flags/feature-flags.json`. Once merged and synced, drop the env
override.

```
   diff (configs/feature-flags/feature-flags.json)
     "o11y-lab0": {
       ...
       "o11y_cloud_integration": "enabled",
   +   "agent_control": "enabled"
     },
```

---

## 3. Set up project, agent stream, and control in the AO UI

Do this once per test org. The screenshots-equivalent steps:

```
   Step 1: Open the AO UI
     https://<console-host>/#/agent-obs
     (lab0 example host: cui-ui-token-2.lab0.observability.splunkcloud.com)

   Step 2: Create / open a Project
     UI: Agent Observability -> Projects -> create or open
     Example: hybim871-ace-demo
       project id: f592350e-414d-4fef-9a1a-a359ebbda38a

   Step 3: Create / open an Agent Stream under that project
     UI: open the project -> Agent Streams -> create or open
     Example: hybim871-e2e
       stream id: 640d0614-0d23-49b3-b33a-589d8908528b

   Step 4: Open the Controls tab for that stream
     UI: project -> agent stream -> Controls tab
     URL shape:
       .../#/agent-obs/project/<project-id>/agent-streams/<stream-id>?view=controls

   Step 5: Add a control (or clone-and-bind an existing one)
     A steer control used here:
       name:      2fa-steer
       step type: tool
       stage:     pre
       evaluator: regex,  pattern [1-9][0-9]{4,}   (matches any integer >= 10000)
       action:    steer   (block and steer, e.g. "2FA required")
     The control must be BOUND to the stream (used_by count > 0) or evaluation matches nothing.
```

You can also create/bind controls via the API (used during this test):

```
   # create a control
   PUT /ao/agent-control/api/v1/controls        (X-SF-Token)
       body: { "name": "...", "data": { execution, scope{step_types,stages},
               condition{selector,evaluator}, action{decision} } }
       -> 200 { "control_id": N }
       (note: create is PUT; POST /controls returns 405 by design)

   # clone an existing control and bind it to a stream
   POST /ao/agent-control/api/v1/controls/<id>/clone-and-bind    (X-SF-Token)
        body: { "target_binding": { "target_type": "log_stream",
                                    "target_id": "<stream-id>" } }
        -> 200 { "id": N, "cloned_from_control_id": <id>, "binding_id": M }
```

---

## 4. Get the three tokens (AO UI, Settings -> Access Tokens)

They are different scopes. Do not mix them.

```
   SF / session token   -> gateway auth (agent-control API, control CRUD, evaluation)
                           sent as header X-SF-Token
   INGEST token         -> splunk-ao OTLP span export (SPLUNK_AO_O11Y_TOKEN)
                           only valid on the ingest endpoint
   API token            -> splunk-ao CRUD, project/stream lookup (SPLUNK_AO_O11Y_API_TOKEN)
```

---

## 5. Install and configure the app

```
   pip install "agent-control-sdk==8.5.0" "splunk-ao"
   (two separate packages; there is NO agent-control-sdk[splunk-ao] extra)

   export SPLUNK_AO_REALM="lab0"
   export SPLUNK_AO_O11Y_TOKEN="<ingest-token>"
   export SPLUNK_AO_O11Y_API_TOKEN="<api-token>"
   export AC_SF_TOKEN="<sf-token>"
   export AC_PROJECT_ID="<project-id>"
   export AC_STREAM_ID="<stream-id>"
   export AC_GATEWAY="https://app.<realm>.signalfx.com/ao/agent-control"
   export AC_AGENT_NAME="<agent-name>"
   export AC_AMOUNT="75000"     # any integer >= 10000 matches the regex control
```

---

## 6. Run the end-to-end test (what happens, step by step)

```
   python agent_control_lab0_e2e.py

   1. app calls the @control-decorated tool  wire_transfer(amount=75000)
        |
   2. @control intercepts BEFORE running the tool, and asks the server:
        POST /ao/agent-control/api/v1/auth/runtime-token-exchange   -> 200 (runtime JWT)
        POST /ao/agent-control/api/v1/evaluation                    -> 200
        |
   3. server evaluates the bound control:
        regex [1-9][0-9]{4,} matches "amount": 75000   -> matched=true, action=steer
        |
   4. @control blocks execution:
        raises ControlSteerError  ("Pattern found", 2FA steer)
        the tool body never runs
        |
   5. the decision is recorded as a telemetry span:
        splunk-ao bridge -> add_control_span -> shares the trace id ->
        POST /ao/agent-control/api/v1/observability/events  -> 202
        span ingested via OTLP
        |
   6. AO UI Tracing tab (Last 15 minutes), open the newest session:

        session
          └─ control node
             ├─ 2fa-steer-clone (control span)   [Triggered]
             ├─ 2fa-steer-clone (control span)   [Triggered]
             └─ execute_tool wire_transfer (tool span)

        control span detail: Controls 1 / Triggered
        output: { "action": "steer", "matched": true, "confidence": 1 }
```

Positive vs negative check:

```
   AC_AMOUNT=75000   ->  matches [1-9][0-9]{4,}  ->  STEER fires, tool blocked
   AC_AMOUNT=5       ->  no match                ->  tool runs normally, is_safe=true
```

---

## 7. The four wiring gotchas (why the control shows in the UI trace)

Miss any one and the session shows Traces 0.

```
   1. observability_sink_name="registered"
      -> control events go to the splunk-ao bridge sink (add_control_span),
         not agent_control's own event store

   2. func.tool_name = "wire_transfer"  (set before @control())
      -> the step is a tool, not llm.  An llm step pulls in the Luna control,
         which errors when the Luna SLM backend is unavailable on lab0

   3. set_trace_context_provider(lambda: TraceContext(trace_id, span_id))
      -> control span shares the splunk-ao trace id.
         get it from logger.current_parent().id AFTER start_trace
         (splunk_ao_context.get_current_trace() returns None; do not use it)

   4. await agent_control.shutdown_observability()  before exit
      -> flushes the background event batcher; otherwise events drop on shutdown

   plus: do NOT use a named start_session; let splunk-ao own the session/trace
```

---

## 8. Version and scope notes

- Agent Control server 8.5.0 (image tag v0.2.74) contains Option A (the configurable
  runtime-token header). rc0 must be bumped to v0.2.74+ before these tests mean anything there.
- O11y Cloud supports splunk-ao (Python) only. The plain galileo SDK is not supported
  (a `/ao/api` 404 from the galileo SDK is expected, not a bug).
- The gateway strips the `/ao/agent-control` and `/ao/api` path prefixes; clients send the
  full path and the gateway forwards the rest to the service.

---

## 9. GA scope and open items (from the 2026-08-25 review)

The control examples in this doc (a Luna prompt-injection control and a regex 2FA-steer
control) are for verifying the flow. They are not a statement of what ships at GA. A couple
of things are still being confirmed with product:

- Supported evaluators shrink at GA. The Controls UI dropdown currently lists roughly 10 to
  20 SLM evaluators, but GA will support only a smaller SLM-based set (around five, to be
  confirmed). Luna / SLM is not supported for the alpha release (target Sept 4). Tracked in
  HYBIM-1006 (clean up the dropdown to the GA-supported set).
- Regex evaluator support is not confirmed. Whether regex-based evaluators are supported for
  Agent Control at GA is an open question. If they are, user-supplied regex must be validated
  (compile and reject invalid patterns before accepting). Tracked in HYBIM-1008. So the regex
  control used here may or may not be in the GA scope.

Enforcement vs telemetry, for clarity:

- agent_control SDK alone gives enforcement. @control runs evaluation and fires a steer
  through the gateway without splunk-ao installed, and agent-control-sdk does not depend on
  splunk-ao.
- splunk-ao is what makes the control execution show up as a span in the O11y UI trace. So
  agent_control alone gives enforcement, and agent_control plus splunk-ao gives enforcement
  plus the control appearing in the UI trace (what this script sets up).
