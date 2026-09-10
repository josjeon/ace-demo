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

For how the SDK and server behave at runtime (evaluation, failure modes, cache
refresh, the span-to-chart rollup, framework integration, multi-tenancy, and
default timeouts), see docs/10_runtime_behavior.md.

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
   +--+--------+-------+-----------+--+   (8)   +-------------+
      |        ^       |           ^  |-------->| Runners-API |
   (4)|     (5)|    (8)|        (7)|  |         | (eval/score |
      v        |       v           |  |         |  execution) |
   +--------+  |    +-------+    +--+--+--+      +-------------+
   |  API   |--+    | Redis |    | Authz  |
   |        |  (6)  | cache+|    |        |      +----------+
   |  CRUD, | flags | event |    | RBAC:  |      |  Wizard  |
   |  /ao/  | config| queue |    | admin  |      | (scorer  |
   |  api   |       +-------+    | vs     |      |  authoring)
   +---+----+                    | runtime|      +----------+
       ^                         +---+----+
    (9)|                             ^
       |                          (7)|
   +---+----+   (10)            +----+---+
   | UI     |<------------------|Console |
   |        |                   |  UI    |
   +--------+                   +--------+
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

   (8) ACS -> Runners-API and ACS/API -> Redis   in the O11y embed, the istio
                    egress config allows agent-control and api to reach
                    runners-api (eval/scorer execution) and Redis. Redis is the AO
                    cache (ElastiCache; the ~5 min Controls-chart cache) and the
                    agent-control control-event queue (RedisEventIngestor).
                    Whether Runners-API itself calls Redis is not verified here.

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
   Runners-API  runs scorer/eval jobs (inferred: produces eval metric scores).
   Wizard       inferred scorer/metric authoring (enabled: false on us1). Role
                taken from the name and the hide_wizard_scorers flag, not wiring.
   Redis        AO cache (Controls-chart rollup, ~5 min TTL) + control-event queue.
   UI/Console   admin and viewing surface.
```

What is verified vs. guessed (read before trusting the arrows):

```
   verified against source:
     - ACS token flow (1,2), ACS <-> Postgres (3), tenant isolation keys
       (namespace_key, target_id): agent-control SDK/engine/server
     - Redis roles: RedisEventIngestor in agent-control server; GALILEO_REDIS_*
       (ElastiCache) in the O11y helm values
     - who MAY call runners-api and Redis: istio egress allow-lists in
       us1/o11y-ao/ao-stack.yaml (agent-control and api egress to both)

   inferred, NOT verified:
     - Runners-API's own outbound calls (e.g. Runners-API -> Redis). Only the
       inbound "ACS/API may call runners-api" is backed.
     - Wizard's connections. ao-stack defines a wizard service (enabled: false on
       us1); its "scorer authoring" role is inferred from the name and the
       hide_wizard_scorers flag, not from any wiring. No Wizard arrow is drawn.
     - source mixing: the hand-drawn box diagram this is based on is Galileo
       OnPrem; the egress/Redis facts above are from the O11y embed (o11y-ao).
       They are assumed similar but the OnPrem topology was not checked.
```

---
