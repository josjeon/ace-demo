#!/usr/bin/env python3
"""
Agent Control lab0 e2e SETUP: create a project, an agent stream, and a bound control.

Run this once before agent_control_lab0_e2e.py. It creates everything the e2e test needs
and prints the IDs to export. Project and agent stream are created with the splunk-ao SDK.
The control is created and bound with the Agent Control server REST API (X-SF-Token),
because splunk-ao has no control-management API.

Prereqs (same as the e2e script):
  pip install "agent-control-sdk==8.5.0" "splunk-ao"
  VPN on, org provisioned, agent_control feature flag ON for the cluster.

Env vars required:
  export SPLUNK_AO_REALM="lab0"
  export SPLUNK_AO_O11Y_TOKEN="<ingest-token>"
  export SPLUNK_AO_O11Y_API_TOKEN="<api-token>"
  export AC_SF_TOKEN="<session-sf-token>"       # gateway auth, sent as X-SF-Token
  export AC_GATEWAY="https://app.<realm>.signalfx.com/ao/agent-control"
  # optional, defaults shown:
  export AC_PROJECT_NAME="qe-agent-control-e2e"
  export AC_STREAM_NAME="qe-agent-control-e2e"

Run:
  python agent_control_lab0_setup.py

It prints, at the end, the export lines to paste before running the e2e script:
  export AC_PROJECT_ID=...
  export AC_STREAM_ID=...
"""

import os
import ssl
import json
import time
import urllib.request


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v


# The control created below: steer any tool input containing an integer >= 10000
# (a "2FA required for large transfers" example). regex evaluator, pre stage, tool step.
# Name is made unique per run (control names must be unique in the org).
CONTROL_DATA = {
    "condition": {
        "selector": {"path": "input"},
        "evaluator": {"name": "regex", "config": {"pattern": "[1-9][0-9]{4,}"}},
    },
    "description": "QE e2e: require 2FA for transfers >= $10,000.",
    "enabled": True,
    "execution": "server",
    "scope": {"step_types": ["tool"], "stages": ["pre"]},
    "action": {
        "decision": "steer",
        "steering_context": {"message": '{"reason":"2FA required"}'},
    },
}


def _ac_request(gateway: str, sf: str, method: str, path: str, body=None):
    """Call the Agent Control server REST API through the gateway with X-SF-Token."""
    url = gateway.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-SF-Token", sf)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # lab0 uses an internal cert chain; the SF token is the real auth here.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def main() -> None:
    # splunk-ao env (project/stream creation goes through the CRUD API token)
    os.environ.setdefault("SPLUNK_AO_REALM", "lab0")
    _require("SPLUNK_AO_O11Y_TOKEN")
    _require("SPLUNK_AO_O11Y_API_TOKEN")
    sf = _require("AC_SF_TOKEN")
    gateway = os.environ.get("AC_GATEWAY", "https://app.lab0.signalfx.com/ao/agent-control")
    project_name = os.environ.get("AC_PROJECT_NAME", "qe-agent-control-e2e")
    stream_name = os.environ.get("AC_STREAM_NAME", "qe-agent-control-e2e")

    from splunk_ao import Project

    # 1. project + agent stream via splunk-ao
    project = Project(name=project_name).create()
    print(f"created project: {project.name} (id {project.id})")
    stream = project.create_agent_stream(stream_name)
    print(f"created agent stream: {stream.name} (id {stream.id})")

    # 2. control via the Agent Control REST API (create is PUT; POST /controls is 405)
    # Control names must be unique in the org, so derive a unique name from the stream.
    control_name = f"qe-2fa-steer-{stream_name}"
    status, resp = _ac_request(gateway, sf, "PUT", "/api/v1/controls",
                               {"name": control_name, "data": CONTROL_DATA})
    if status != 200:
        raise SystemExit(f"control create failed: {status} {resp}")
    control_id = resp.get("control_id")
    print(f"created control: {control_name} (id {control_id})")

    # 3. bind the control to the agent stream (clone-and-bind)
    status, resp = _ac_request(gateway, sf, "POST", f"/api/v1/controls/{control_id}/clone-and-bind",
                               {"target_binding": {"target_type": "log_stream", "target_id": stream.id}})
    if status != 200:
        raise SystemExit(f"clone-and-bind failed: {status} {resp}")
    print(f"bound control -> stream: binding_id {resp.get('binding_id')}, "
          f"clone control_id {resp.get('id')}")

    print()
    print("Setup done. Export these before running agent_control_lab0_e2e.py:")
    print(f"  export AC_PROJECT_ID={project.id}")
    print(f"  export AC_STREAM_ID={stream.id}")


if __name__ == "__main__":
    main()
