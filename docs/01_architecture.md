# How it fits together (component map)

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
        |    through untouched                      |
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

Key idea: the gateway owns the `Authorization` header for its identity JWT, so the
Agent Control runtime token rides a separate header, `X-Agent-Control-Runtime-Token`. The two
never collide.

Which SDK where:

```
  O11y Cloud (SaaS, lab0/rc0):  splunk-ao SDK only   (galileo SDK NOT supported)
  OnPrem:                       galileo SDK (to be discontinued) + splunk-ao
  agent_control SDK:            separate, enforcement, works via the gateway in both
```

The agent_control SDK is always local, embedded in your process. What changes
between OnPrem, Galileo cloud, and the O11y embed is only where the Agent Control
server (ACS) lives and which credential you present to the gateway.

---

## Runtime behavior (verified against agent-control and orbit source)

The rest of this file describes how the SDK and server behave at runtime. For
token scopes see docs/04_tokens_and_env.md; for wiring mistakes see
docs/06_gotchas.md.

### When an evaluation fires

The framework hook fires on every model and tool call boundary, but an
evaluation only runs when a bound control's scope matches that step. Scope is
`{step_types, stages}`:

```
   step_types:  llm  |  tool
   stages:      pre  (before the call, on input)  |  post (after, on output)
```

A `llm/pre` control evaluates before every LLM call; a `tool/pre` control
evaluates before every tool call. A step with no matching control is a no-op.
Steps whose matching controls are all `execution: sdk` are evaluated locally with
no network hop; steps that need a server-side control call ACS.

`pre` blocks a bad request before it runs, and before you pay for the model call.
`post` inspects the output after the fact. `steer` is a `pre` concept: it tells
the agent to correct course (for example, require 2FA) and retry.

### Multiple controls on one step

One evaluation call handles the whole matching set, not one call per control.

```
   engine launches all matching controls as concurrent asyncio tasks
   AGENT_CONTROL_MAX_CONCURRENT_EVALUATIONS (default 3) caps concurrent LEAF
     evaluator executions across those tasks, NOT the number of top-level
     controls; composite condition trees are still walked serially
   first deny match cancels the remaining tasks
   each evaluator runs under EVALUATOR_TIMEOUT_SECONDS (default 30s)
   result carries: matches, non_matches, errors (both deny and steer errors)
```

How the engine sets `is_safe`:

```
   deny match     -> is_safe = false  (block)
   steer match    -> is_safe = false  (steer / retry)
   deny errored   -> is_safe = false  (fail-closed: safety could not be verified)
   steer errored  -> is_safe stays true, logged as non-blocking
   otherwise      -> is_safe = true   (pass)
```

The important case is an erroring deny control: it fails closed and blocks. An
erroring steer control does not change `is_safe` at the engine level.

Caveat on the enforcement path: the `integrations/_core.py` helper raises on any
non-empty `result.errors`, so through that path even a steer error blocks. The
engine (non-blocking steer errors) and that helper (raise on any error) disagree,
so steer-error behavior depends on which enforcement path you use. Verify against
your integration before relying on it.

Evaluation order does not matter, outcome type does. With several
`execution: server` controls on a stream, a deny control that errors blocks the
step, so availability is only as good as the flakiest deny control.

### Failure behavior: fail-closed

If the SDK cannot reach ACS, or the server returns a non-2xx, the exception
propagates and the step is blocked. Agent Control is a hard dependency in the
request path when a server-side control applies. A local `execution: sdk` control
that already decides `not is_safe` short-circuits without a server call.

A server-side control whose backend is unreachable (for example a Galileo scorer
that is down) does not fail cleanly. It hangs until the evaluator timeout, then
surfaces as an error. Whether that error blocks depends on the action: a deny
control's error fails closed and blocks; a steer control's error is non-blocking
at the engine level. Prefer `execution: sdk` for controls that must evaluate
reliably in an isolated environment.

### Control cache and refresh

`init()` calls `initAgent`, which returns the controls bound to the target and
caches them. A background thread re-pulls them every
`policy_refresh_interval_seconds` (default 60s), so a control change in the UI
takes effect within about a minute without a restart. Set the interval to 0 to
disable the loop. A failed refresh logs and keeps the existing cache, so a
refresh outage leaves controls stale rather than blocking.

For `initAgent` to return the bound control, the agent must declare the guarded
step at init (`steps=[{"type": "tool", "name": "..."}]`). Without a matching step
the cache stays empty. When the cache is empty the SDK makes no server call and
the step returns `is_safe=true`: it passes silently. A misconfigured init
(missing step declaration) therefore fails open, not closed, which is the
dangerous mode. This is the same root cause as gotcha 2 in docs/06_gotchas.md,
seen from the init side.

### From control span to the Controls chart

Two stages, both on the O11y side, doing different jobs.

Stage 1, ingest-service (normalize and store, no rollup): the OTLP
`agent_control.control_execution` span is mapped to a typed control record
(`otel_record.go` reads `agent_control.action`, `.matched`, `.control_id`,
`.evaluator_name`, `.selector_path`, `.check_stage`, and the input) and written to
ClickHouse. This stage decides whether the span becomes a queryable
`type=control` record at all. An ingest build without Agent Control OTEL support
stores the span but types it as `workflow`, so nothing downstream finds it.

Stage 2, AO API (rollup at query time): the Controls chart runs a ClickHouse
`GROUP BY` over the stored control spans, grouping by dimensions defined in
`control_trends.py` (Control Name, Check Stage, Applies To, Evaluator Name,
Selector Path, Action, Matched). Results are bucketed and cached with a five
minute TTL. The chart counts executions, not unique controls, so a control that
fires twelve times shows twelve.

Two consequences worth knowing:

```
   sending more control spans moves the chart (it is a live count)
   beyond span indexing lag, the chart can trail another ~5 min (rollup cache)
```

Scorer and eval metrics use a separate rollup driven by a compute pipeline that
ingest triggers over Kafka or Celery. The Controls chart does not use it.

### Two ways to render the control span

```
   Logger path (SplunkAOLogger):  SDK builds the typed control span client-side
                                  and exports it. Reliable across realms.
   OTLP path:                     SDK emits a raw agent_control.control_execution
                                  span; ingest normalizes it. May not produce the
                                  span if the ingest build lacks AC OTEL support.
```

A standalone-emitted control span has correct output (action, matched,
confidence) but a blank Input Text, because the input does not persist onto the
control span through hydration. The prompt is still visible at the
workflow/session level. To populate the control span's own input, emit it from
inside the app's real trace (nested under the app's llm span) rather than
hydrating it separately.

### Framework integration

The SDK ships plugins for Strands and Google ADK plus a framework-agnostic
`@control()` decorator, and there is a LangChain example under `examples/`.
`init()` connects the SDK to ACS and registers the agent; it does not wire any
framework. Attaching the plugin is a separate step, and it differs by framework.

Google ADK:

```python
agent_control.init(agent_name="my-agent", server_url=..., steps=[...])
plugin = AgentControlPlugin(agent_name="my-agent")   # ADK plugin: __init__ raises
                                                     # if agent_name != init's agent
plugin.bind(root_agent)   # discovers steps and pre-syncs them to ACS before the
                          # runner starts. It does NOT attach event hooks; ADK
                          # wires the before/after model and tool callbacks through
                          # its own BasePlugin protocol.
```

Strands: the plugin has no `bind()`. It exposes `init_agent(agent)`, which the
Strands framework calls automatically when the plugin is registered with an
agent. The Strands plugin also does NOT check that `agent_name` matches
`init()`; a mismatched name silently evaluates under the wrong agent, so the
caller must keep them in sync.

However the plugin is attached, when a lifecycle event fires (before/after model,
tool, node) it calls the SDK's evaluate-and-enforce, which blocks or steers by
raising.

### Multi-tenancy isolation

Two levels, both enforced server-side:

```
   namespace_key (org/tenant):  every control query filters on
                                Control.namespace_key == namespace_key.
                                The key comes from the authenticated principal and
                                is a claim in the runtime token, so one namespace's
                                controls are invisible to another.
   target_id (log stream):      the runtime token is bound to a stream, and the
                                exchange rejects a request whose target does not
                                match the principal's.
```

### Default timeouts and limits

```
   SDK to ACS HTTP timeout        30s      (client.py)
   per-evaluator timeout          30s      (EVALUATOR_TIMEOUT_SECONDS, engine core.py)
   concurrent leaf evaluations    3        (AGENT_CONTROL_MAX_CONCURRENT_EVALUATIONS, engine core.py)
   control refresh interval       60s      (policy_refresh_interval_seconds, init())
   observability export retries   3        (delay 1s per attempt, settings.py)
```

Steer retry is not an SDK setting. The app decides how many times to retry a
steered step before giving up (this demo defaults to 3 via `--max-steer-attempts`).

---

## Inside the Galileo cluster (OnPrem component map)

This is the OnPrem deployment: the whole Galileo stack runs in one cluster and
the agent's SDK calls in from outside. In the O11y embed the same components run
in the `o11y-ao` namespace and the O11y gateway fronts them (see the top of this
file); the internal wiring below is the same.

Numbers on the arrows are explained under the diagram.

```
   Agent (outside the cluster)
   +-----------+
   |  SDK      |
   +-----+-----+
     |  (1) GalileoAPIKey / X-SF-Token        ^  (2) runtime JWT
     v  POST /auth/runtime-token-exchange     |  (short-lived, target-bound)
   ==|=========================================|==============  Galileo Cluster ==
     v                                         |
   +-------------------------------+   (3)   +-----------------+
   |  ACS (Agent Control Server)   |-------->|  Postgres DB    |
   |  - mints runtime JWT          |  read/  |  controls,      |
   |  - evaluates the control set  |  write  |  bindings,      |
   |  - execution=sdk local /      |         |  agents         |
   |    execution=server here      |         +-----------------+
   +--+--------+----------------+--+
      |        ^                ^
   (4)|     (5)|             (7)|  authz check
      v        |                |  (may control call)
   +--------+  |             +--------+        +-------------+   +----------+
   |  API   |--+             | Authz  |        | Runners-API |   |  Wizard  |
   |        |  (6) flags,    |        |        | (eval/score |   | (scorer  |
   |  CRUD, |  config,       | RBAC:  |        |  execution) |   |  authoring)
   |  /ao/  |  control CRUD  | admin  |        +------+------+   +----------+
   |  api   |                | vs     |               |
   +---+----+                | runtime|         (8)   v
       ^                     +---+----+        +-------------+
    (9)|                         ^             |   Redis     |
       |                      (7)|             | cache +     |
   +---+----+   (10)         +---+----+        | event queue |
   | UI     |<---------------|Console |        +-------------+
   |        |                |  UI    |
   +--------+                +--------+
```

Critical arrows:

```
   (1) SDK -> ACS   auth with the Galileo API key (X-SF-Token in the O11y embed).
                    This is the ONLY credential the agent app holds. In the O11y
                    embed it must carry API scope for the gateway; a separate
                    INGEST token handles span export (docs/04_tokens_and_env.md).

   (2) ACS -> SDK   returns a runtime JWT: scope runtime.use, bound to
                    (namespace_key, target_id=log_stream), expires in minutes.
                    The SDK sends it back on X-Agent-Control-Runtime-Token for
                    each /evaluation call, never on Authorization.

   (3) ACS -> Postgres    control definitions, bindings, and agent registrations.
                    initAgent reads the target's bound controls from here; the
                    60s refresh loop re-reads them.

   (4) API -> ACS / (5) ACS -> API   the app API and ACS exchange control CRUD,
                    flags, and configuration. Creating or editing a control goes
                    through the API; ACS consumes the resulting definitions.

   (6) UI/Console -> API   admins manage controls and view results through the
                    API. Console UI is the OnPrem console; in the O11y embed this
                    is the AO UI.

   (7) ACS -> Authz and UI -> Authz   authorization. Authz enforces who may do
                    what: only admins mutate controls, runtime principals may only
                    fetch and evaluate their assigned controls. Isolation keys are
                    namespace_key (org/tenant) and target_id (stream). (RBAC split
                    of admin vs runtime scope is a documented direction; verify the
                    exact enforcement points before relying on them.)

   (8) Runners-API -> Redis   eval/scorer execution. Runners-API runs scorer and
                    metric jobs (the separate eval rollup, not the Controls chart).
                    Redis is both the AO cache (ElastiCache; the ~5 min Controls
                    chart cache lives here) and the control-event queue
                    (RedisEventIngestor pushes control events for async workers).

   (9) API <-> UI    the UI reads spans, controls, and chart data from the API.

   (10) Console UI -> UI   entry point to the AO/Agent Control views.
```

Component roles, one line each:

```
   ACS          the enforcement engine: mints runtime tokens, evaluates controls,
                returns steer/deny/allow. What the SDK talks to.
   API          app API: control CRUD, feature flags, /ao/api/configuration,
                span readback, the Controls-chart rollup query.
   Authz        RBAC and tenant isolation (namespace_key, target_id).
   Postgres     source of truth for controls, bindings, agents.
   Runners-API  runs scorer/eval jobs (produces eval metric scores).
   Wizard       authors scorers/metrics that evaluators (e.g. galileo.luna) use.
                Not the control engine; a producer of scorers the controls consume.
   Redis        AO cache (Controls-chart rollup, ~5 min TTL) + control-event queue.
   UI/Console   admin and viewing surface.
```

Note on what is verified vs. inferred: the ACS, API, Postgres, Authz, Redis
roles and the token/isolation flows are confirmed against agent-control and
orbit source. The exact Runners-API and Wizard wiring is taken from the OnPrem
component diagram and the service definitions in the helm values; treat those two
boxes' internal arrows as approximate.

---
