# Runtime behavior (verified against agent-control and orbit source)

How the SDK and server behave at runtime: when an evaluation fires, what happens
with multiple controls, how it fails, and how a control decision becomes a chart.
For the component map and cluster diagram see docs/01_architecture.md; for token
scopes see docs/04_tokens_and_env.md; for wiring mistakes see docs/06_gotchas.md.

## When an evaluation fires

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

## Multiple controls on one step

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

## Failure behavior: fail-closed

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

## Control cache and refresh

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

## From control span to the Controls chart

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

## Two ways to render the control span

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

## Framework integration

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

## Multi-tenancy isolation

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

## Default timeouts and limits

```
   SDK to ACS HTTP timeout        30s      (client.py)
   per-evaluator timeout          30s      (EVALUATOR_TIMEOUT_SECONDS, engine core.py)
   concurrent leaf evaluations    3        (AGENT_CONTROL_MAX_CONCURRENT_EVALUATIONS, engine core.py)
   control refresh interval       60s      (policy_refresh_interval_seconds, init())
   observability export retries   3        (delay 1s per attempt, settings.py)
```

Steer retry is not an SDK setting. The app decides how many times to retry a
steered step before giving up (this demo defaults to 3 via `--max-steer-attempts`).
