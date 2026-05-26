# AGENTS.md

Reference for AI coding agents working in this standalone Agent Control + Galileo E2E demo.

## Project Purpose

This folder contains a small Python demo that proves Agent Control evaluation events can be converted into Galileo control spans, ingested into a Galileo devstack, read back through trace/span APIs, and queried by Trends custom metrics APIs.

Main files:

- `setup_controls.py`: creates the demo Agent Control controls when explicitly needed.
- `run_demo.py`: runs the demo agent flow, emits control events, verifies Galileo readback, and queries Trends APIs.
- `common.py`: configures default URLs and credentials.

## Sensitive Data

- Never write real `GALILEO_API_KEY` values into files, logs, commits, or final summaries.
- Use environment variables for credentials.
- When sharing commands, use `<your-api-key>` placeholders.

## Local Python Environment

The demo should not require local `agent-control` or `orbit` checkouts. Agent Control runs as a devstack service, and Orbit is already installed in the devstack. Locally, use a Python environment with the Galileo Python SDK installed.

The default demo interpreter is:

```bash
~/code/agent-control-galileo-e2e/.venv/bin/python
```

## Environment

Typical devstack environment:

```bash
export GALILEO_API_KEY="<your-api-key>"
export GALILEO_CONSOLE_URL="https://console-test-evals.gcp-dev.galileo.ai"
export GALILEO_API_URL="https://api-test-evals.gcp-dev.galileo.ai"
export GALILEO_PROJECT="agent-control-banking-demo-project"
export GALILEO_LOG_STREAM="agent-control-banking-demo-logstream"
export AGENT_CONTROL_URL="https://agent-control-test-evals.gcp-dev.galileo.ai"
export AGENT_CONTROL_AGENT_NAME="galileo-control-span-demo"
export AGENT_CONTROL_TARGET_TYPE="log_stream"
```

Agent Control is expected to run as a devstack service. Do not require FDEs to start a local Agent Control server for this package.

Health check:

```bash
curl -sS -m 5 "$AGENT_CONTROL_URL/health"
```

Expected:

```json
{"status":"healthy","version":"7.6.0"}
```

## Run The Demo

From this folder:

```bash
cd /Users/namrataghadi/code/agent-control-galileo-e2e

GALILEO_API_KEY="<your-api-key>" \
GALILEO_CONSOLE_URL="https://console-test-evals.gcp-dev.galileo.ai" \
GALILEO_API_URL="https://api-test-evals.gcp-dev.galileo.ai" \
GALILEO_PROJECT="agent-control-banking-demo-project" \
GALILEO_LOG_STREAM="agent-control-banking-demo-logstream" \
AGENT_CONTROL_URL="https://agent-control-test-evals.gcp-dev.galileo.ai" \
AGENT_CONTROL_AGENT_NAME="galileo-control-span-demo" \
AGENT_CONTROL_TARGET_TYPE="log_stream" \
"$DEMO_PYTHON" run_demo.py \
  --verify-api \
  --query-trends \
  --verify-delay-seconds 10 \
  --console-url https://console-test-evals.gcp-dev.galileo.ai \
  --api-base-url https://api-test-evals.gcp-dev.galileo.ai \
  --server-url https://agent-control-test-evals.gcp-dev.galileo.ai
```

Expected high-level output:

- `Automatic Galileo Agent Control bridge: enabled`
- `Registered Agent Control sinks: 1`
- 6 Agent Control banking controls configured:
  - `demo-observe-luna-transfer-request`
  - `demo-deny-sanctioned-country-transfer`
  - `demo-deny-high-fraud-risk-transfer`
  - `demo-observe-new-recipient-transfer`
  - `demo-steer-large-transfer-2fa`
  - `demo-steer-manager-approval`
- `Control spans attached during run: <non-zero>`
- `control_spans_in_trace_payload=<non-zero>`
- `control_spans_in_partial_search_for_trace=<non-zero>`
- Trends API verification with non-zero custom metric results.

## Trends API Checks

If the script succeeds, it prints:

- `dashboard_id`
- `dashboard_name=Default View`
- `dashboard_sections=1`
- custom metrics for:
  - control counts by name and stage
  - tool pre-control match status
  - post-tool matched actions
  - LLM selector paths

Counts increase as the demo is rerun. Do not hard-code exact counts in permanent assertions unless the test environment is reset first.

## Troubleshooting

### 1. Confirm The GKE Context And Namespace

```bash
kubectl config current-context
kubectl get namespaces
```

Expected context:

```text
gke_galileo-stacks-dev_us-central1_gke-us-central1
```

For the `test-evals` stack, use namespace:

```bash
export NS="test-evals"
```

### 2. Check Stack Pods Are Up

```bash
kubectl get pods -n "$NS" -o wide
```

Important pods should be `Running`:

- `api-...`
- `ingest-service-...`
- `data-service-...`
- `postgres-v16-0`
- `chi-clickhouse-cluster-0-0-0`
- `chi-clickhouse-cluster-0-1-0`
- `rabbitmq-cluster-server-0`
- `redis-*`

If the demo is run while pods are still starting or rolling, the session row may be created while trace/span ingest fails or is not persisted. In that case rerun the demo after the stack stabilizes.

Check rollout and scheduling events:

```bash
kubectl get events -n "$NS" --sort-by=.lastTimestamp
```

### 3. Check Remote Postgres

Pod readiness:

```bash
kubectl get pod -n "$NS" postgres-v16-0 -o wide
```

Service and endpoint:

```bash
kubectl get svc -n "$NS" postgres-v16
kubectl get endpoints -n "$NS" postgres-v16
```

Readiness inside the pod:

```bash
kubectl exec -n "$NS" postgres-v16-0 -- pg_isready -h 127.0.0.1 -p 5432
```

Expected:

```text
127.0.0.1:5432 - accepting connections
```

TCP reachability from the API pod:

```bash
API_POD="$(kubectl get pod -n "$NS" -l app=api -o jsonpath='{.items[0].metadata.name}')"
kubectl exec -n "$NS" "$API_POD" -- python -c "import socket; socket.create_connection(('postgres-v16', 5432), 5).close(); print('postgres-v16:5432 reachable from api pod')"
```

Expected:

```text
postgres-v16:5432 reachable from api pod
```

### 4. Query ClickHouse For A Trace

Use the trace, project, and log stream IDs printed by `run_demo.py`:

```bash
export TRACE_ID="<trace-id>"
export PROJECT_ID="<project-id>"
export LOG_STREAM_ID="<log-stream-id>"
```

List ClickHouse databases and tables:

```bash
kubectl exec -n "$NS" chi-clickhouse-cluster-0-0-0 -- clickhouse-client --query "SHOW DATABASES"
kubectl exec -n "$NS" chi-clickhouse-cluster-0-0-0 -- clickhouse-client --database galileo --query "SHOW TABLES"
```

Trace rows in the raw table:

```bash
kubectl exec -n "$NS" chi-clickhouse-cluster-0-0-0 -- clickhouse-client --database galileo --query "
SELECT
  id,
  trace_id,
  type,
  name,
  parent_id,
  control_id,
  check_stage,
  applies_to,
  agent_name,
  created_at
FROM log_records_replicated
WHERE trace_id = toUUID('$TRACE_ID')
ORDER BY created_at, type
FORMAT Vertical"
```

Trace rows in the materialized view:

```bash
kubectl exec -n "$NS" chi-clickhouse-cluster-0-0-0 -- clickhouse-client --database galileo --query "
SELECT
  id,
  trace_id,
  type,
  name,
  parent_id,
  control_id,
  check_stage,
  applies_to,
  agent_name,
  created_at
FROM log_records_mv_replicated
WHERE trace_id = toUUID('$TRACE_ID')
ORDER BY created_at, type
FORMAT Vertical"
```

Expected successful trace types:

- `trace`
- `workflow`
- `llm`
- `tool`
- `control`

Expected control fields:

- `type=control`
- `control_id` populated
- `check_stage` is `pre` or `post`
- `applies_to` is `llm_call` or `tool_call`
- `agent_name=galileo-control-span-demo`

If a project query only shows `type=session` and no trace/span rows, the session was created but the trace/span ingest did not persist. Check stack readiness and ingest logs, then rerun the demo after the stack is stable.

Summarize records by type:

```bash
kubectl exec -n "$NS" chi-clickhouse-cluster-0-0-0 -- clickhouse-client --database galileo --query "
SELECT type, count()
FROM log_records_replicated
WHERE project_id = toUUID('$PROJECT_ID')
GROUP BY type
ORDER BY type
FORMAT PrettyCompact"
```

### 5. Inspect Service Logs

API logs:

```bash
API_POD="$(kubectl get pod -n "$NS" -l app=api -o jsonpath='{.items[0].metadata.name}')"
kubectl logs -n "$NS" "$API_POD" --since=40m --tail=500
```

Ingest service logs:

```bash
INGEST_POD="$(kubectl get pod -n "$NS" -l app=ingest-service -o jsonpath='{.items[0].metadata.name}')"
kubectl logs -n "$NS" "$INGEST_POD" --since=40m --tail=500
```

Data service logs:

```bash
DATA_POD="$(kubectl get pod -n "$NS" -l app=data-service -o jsonpath='{.items[0].metadata.name}')"
kubectl logs -n "$NS" "$DATA_POD" --since=40m --tail=500
```

Look for:

- HTTP `422` from trace ingest
- startup/readiness probe failures
- ClickHouse insert errors
- RabbitMQ publish/consume errors
- API pod rolling during the demo run

### 6. Confirm Public API Readback

```bash
export API_BASE="https://api-test-evals.gcp-dev.galileo.ai"
export GALILEO_API_KEY="<your-api-key>"
export TOKEN="$(
  curl -sS -X POST "$API_BASE/login/api_key" \
    -H "Content-Type: application/json" \
    -d "{\"api_key\":\"$GALILEO_API_KEY\"}" | jq -r ".access_token"
)"

curl -sS "$API_BASE/projects/$PROJECT_ID/traces/$TRACE_ID" \
  -H "Authorization: Bearer $TOKEN" | jq "{id, type, control_spans: ([.. | objects | select(.type? == \"control\")] | length)}"
```

Expected:

```json
{
  "id": "<trace-id>",
  "type": "trace",
  "control_spans": 3
}
```

Partial span search:

```bash
curl -sS -X POST "$API_BASE/projects/$PROJECT_ID/spans/partial_search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF | jq "{num_records, records: [.records[] | {type, name, id, trace_id, control_id, check_stage}]}"
{
  "log_stream_id": "$LOG_STREAM_ID",
  "filter_tree": {
    "filter": {"name": "trace_id", "operator": "eq", "value": "$TRACE_ID", "type": "text"}
  },
  "pagination": {"limit": 20},
  "select_columns": {
    "column_ids": ["id", "trace_id", "type", "name", "control_id", "check_stage"],
    "include_all_metrics": false,
    "include_all_feedback": false
  }
}
EOF
```

Expected:

- `num_records` greater than `0`
- three records with `type=control`

### 7. Instrument Trace Ingest Status If Needed

The SDK treats telemetry ingest as resilient and may swallow infrastructure errors. If readback returns `404`, rerun with SDK logging enabled or temporarily monkeypatch `IngestTraces.ingest_traces` in a one-off local script to print:

- ingest URL
- trace ID
- record type counts
- HTTP status
- response body

A healthy ingest response looks like:

```json
{
  "log_stream_id": "<log-stream-id>",
  "project_id": "<project-id>",
  "session_id": "<session-id>",
  "records_count": 7,
  "traces_count": 1
}
```

If the response is `422`, the response body usually identifies the rejected field or span type. If the response is `200` but ClickHouse has no trace rows, inspect data-service and RabbitMQ consumers.
