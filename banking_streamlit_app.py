#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from common import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_AGENT_NAME,
    DEFAULT_API_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)
from run_demo import (
    DEFAULT_BANKING_PROMPT,
    _apply_steering_context,
    _compute_fraud_score,
    _describe_steering,
    _draft_transfer_plan_impl,
    _login_with_api_key,
    _parse_transfer_request,
    _control_exception_message,
    _control_exception_rows,
    _should_skip_luna_control,
    draft_transfer_plan,
    process_wire_transfer,
    _render_final_answer,
    _verify_bound_controls,
)
from setup_controls import control_specs


STREAMLIT_SCENARIOS: dict[str, dict[str, Any]] = {
    "AND + NOT - 2FA steering": {
        "prompt": DEFAULT_BANKING_PROMPT,
        "amount": 0.0,
        "destination": "",
        "recipient": "",
        "fraud_score": 0.0,
        "expected_status": "completed",
        "expected_steers": 1,
        "expected_verified_2fa": True,
        "expected_control": "demo-steer-large-transfer-2fa",
        "expected_min_control_spans": 4,
        "expectation": "One steer sets verified_2fa=true, then the retry completes.",
    },
    "OR - sanctioned country": {
        "prompt": "Wire $5,000 to Horizon Robotics in Iran for invoice INV-2026-015.",
        "amount": 5000.0,
        "destination": "Iran",
        "recipient": "",
        "fraud_score": 0.10,
        "expected_status": "blocked",
        "expected_steers": 0,
        "expected_verified_2fa": False,
        "expected_control": "demo-deny-risky-transfer-composite",
        "expected_min_control_spans": 2,
        "expectation": "The sanctioned-country OR branch denies the transfer.",
    },
    "OR - high fraud score": {
        "prompt": "Wire $5,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-016.",
        "amount": 5000.0,
        "destination": "United Kingdom",
        "recipient": "",
        "fraud_score": 0.95,
        "expected_status": "blocked",
        "expected_steers": 0,
        "expected_verified_2fa": False,
        "expected_control": "demo-deny-risky-transfer-composite",
        "expected_min_control_spans": 2,
        "expectation": "The fraud-score OR branch denies the transfer.",
    },
    "No composite match": {
        "prompt": "Wire $5,000 to Horizon Robotics in the United Kingdom for invoice INV-2026-017.",
        "amount": 5000.0,
        "destination": "United Kingdom",
        "recipient": "",
        "fraud_score": 0.10,
        "expected_status": "completed",
        "expected_steers": 0,
        "expected_verified_2fa": False,
        "expected_control": None,
        "expected_min_control_spans": 2,
        "expectation": "Neither composite control matches; the transfer completes unchanged.",
    },
}


def _scenario_failures(scenario: dict[str, Any], result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if result["status"] != scenario["expected_status"]:
        failures.append(f"status={result['status']!r}, expected {scenario['expected_status']!r}")

    steer_count = len(result["steering_history"])
    if steer_count != scenario["expected_steers"]:
        failures.append(f"steers={steer_count}, expected {scenario['expected_steers']}")

    verified_2fa = bool(result["final_transfer"].get("verified_2fa"))
    if verified_2fa != scenario["expected_verified_2fa"]:
        failures.append(
            f"final verified_2fa={verified_2fa}, expected {scenario['expected_verified_2fa']}"
        )

    expected_control = scenario["expected_control"]
    if expected_control and expected_control not in result["answer"]:
        failures.append(f"response did not identify control {expected_control!r}")

    expected_min_spans = scenario["expected_min_control_spans"]
    if "control_span_count" in result:
        control_span_count = int(result["control_span_count"])
        if control_span_count < expected_min_spans:
            failures.append(
                f"control spans={control_span_count}, expected at least {expected_min_spans}"
            )
        persisted_control_span_count = int(result.get("persisted_control_span_count", 0))
        if persisted_control_span_count < expected_min_spans:
            failures.append(
                f"persisted control spans={persisted_control_span_count}, "
                f"expected at least {expected_min_spans}"
            )
        control_names = result.get("persisted_control_names", [])
    elif "raw_control_events" in result and "typed_control_spans" in result:
        raw_control_events = int(result["raw_control_events"])
        typed_control_spans = int(result["typed_control_spans"])
        if raw_control_events < expected_min_spans:
            failures.append(
                f"persisted OTEL control events={raw_control_events}, "
                f"expected at least {expected_min_spans}"
            )
        if typed_control_spans < expected_min_spans:
            failures.append(
                f"persisted typed control spans={typed_control_spans}, "
                f"expected at least {expected_min_spans}"
            )
        control_names = result.get("persisted_control_names", [])
    else:
        failures.append("result did not include a supported control telemetry count")
        control_names = []

    if expected_control and not any(
        expected_control in str(control_name) for control_name in control_names
    ):
        failures.append(f"control telemetry did not identify {expected_control!r}")
    return failures


def _masked_api_key() -> str:
    api_key = os.environ.get("GALILEO_API_KEY", "")
    if len(api_key) <= 8:
        return "unset" if not api_key else "***"
    return f"{api_key[:4]}...{api_key[-4:]}"


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=os.environ.get("AGENT_CONTROL_AGENT_NAME", DEFAULT_AGENT_NAME),
        server_url=os.environ.get("AGENT_CONTROL_URL", DEFAULT_AGENT_CONTROL_URL),
        project=os.environ.get("GALILEO_PROJECT", DEFAULT_PROJECT),
        log_stream=os.environ.get("GALILEO_LOG_STREAM", DEFAULT_LOG_STREAM),
        mode=os.environ.get("GALILEO_LOGGER_MODE", "batch"),
        console_url=os.environ.get("GALILEO_CONSOLE_URL", DEFAULT_CONSOLE_URL),
        api_base_url=os.environ.get("GALILEO_API_URL", DEFAULT_API_URL),
        runtime_auth_mode=os.environ.get("AGENT_CONTROL_RUNTIME_AUTH_MODE", "jwt"),
        target_type=os.environ.get("AGENT_CONTROL_TARGET_TYPE", "log_stream"),
        skip_runtime_token_check=True,
        skip_scorer_invoke_check=True,
        skip_luna_control=None,
        expect_luna_deny=False,
        max_steer_attempts=3,
    )


def _configure_environment(args: SimpleNamespace) -> None:
    if args.console_url:
        os.environ["GALILEO_CONSOLE_URL"] = args.console_url
    if args.api_base_url:
        os.environ["GALILEO_API_URL"] = args.api_base_url
    os.environ["GALILEO_PROJECT"] = args.project
    os.environ["GALILEO_LOG_STREAM"] = args.log_stream
    os.environ["AGENT_CONTROL_URL"] = args.server_url
    os.environ["AGENT_CONTROL_RUNTIME_AUTH_MODE"] = args.runtime_auth_mode
    resolve_agent_control_api_key()
    resolve_agent_control_api_key_header()


async def _validate_galileo_credentials(args: SimpleNamespace) -> None:
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GALILEO_API_KEY in the Streamlit process environment.")

    async with httpx.AsyncClient(timeout=20.0) as client:
        await _login_with_api_key(client, args.api_base_url.rstrip("/"), api_key)


async def _readback_trace_records(
    args: SimpleNamespace,
    *,
    project_id: str,
    log_stream_id: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    api_key = os.environ.get("GALILEO_API_KEY")
    if not api_key:
        return []

    latest_records: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        bearer_headers = await _login_with_api_key(client, args.api_base_url.rstrip("/"), api_key)
        for attempt in range(10):
            try:
                response = await client.post(
                    f"{args.api_base_url.rstrip('/')}/projects/{project_id}/spans/partial_search",
                    headers=bearer_headers,
                    json={
                        "log_stream_id": log_stream_id,
                        "filter_tree": {
                            "filter": {
                                "name": "trace_id",
                                "operator": "eq",
                                "value": trace_id,
                                "type": "text",
                            }
                        },
                        "pagination": {"limit": 100},
                        "select_columns": {
                            "column_ids": ["id", "trace_id", "type", "name", "control_id"],
                            "include_all_metrics": False,
                            "include_all_feedback": False,
                        },
                    },
                )
                response.raise_for_status()
                records = response.json().get("records", [])
                unique_records = {
                    str(record.get("id")): record
                    for record in records
                    if isinstance(record, dict)
                }
                latest_records = list(unique_records.values())
                if any(record.get("type") == "control" for record in latest_records):
                    return latest_records
            except httpx.HTTPError:
                if attempt == 9:
                    return latest_records
            await asyncio.sleep(1.0)
    return latest_records


async def _run_banking_agent(
    *,
    args: SimpleNamespace,
    prompt: str,
    amount: float | None,
    destination_country: str | None,
    recipient_name: str | None,
    fraud_score: float | None,
) -> dict[str, Any]:
    _configure_environment(args)
    await _validate_galileo_credentials(args)

    import agent_control
    from agent_control import ControlSteerError, ControlViolationError
    from galileo.logger.logger import GalileoLogger

    logger = GalileoLogger(project=args.project, log_stream=args.log_stream, mode=args.mode)
    session_id = logger.start_session(
        name="agent-control-banking-streamlit",
        external_id=f"agent-control-banking-streamlit-{uuid4()}",
        metadata={"demo": "agent-control-banking-streamlit"},
    )
    if logger.project_id is None or logger.log_stream_id is None:
        raise RuntimeError("Galileo logger did not resolve project/log stream IDs.")

    os.environ["GALILEO_PROJECT_ID"] = logger.project_id
    target_id = logger.log_stream_id
    target_type = args.target_type

    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Interactive banking transfer demo for Agent Control + Galileo",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="registered",
        target_type=target_type,
        target_id=target_id,
    )

    control_config_args = argparse.Namespace(**vars(args))
    control_config_args.target_type = target_type
    await _verify_bound_controls(control_config_args, target_type=target_type, target_id=target_id)

    parse_args = SimpleNamespace(
        prompt=prompt,
        amount=amount,
        destination_country=destination_country or None,
        recipient_name=recipient_name or None,
        fraud_score=fraud_score,
    )
    transfer = _parse_transfer_request(parse_args)
    transfer["fraud_score"] = _compute_fraud_score(transfer, fraud_score)

    trace_input = {"prompt": prompt, "transfer": transfer}
    trace = logger.start_trace(input=trace_input, name="agent-control-banking-streamlit")
    workflow = logger.add_workflow_span(input=json.dumps(trace_input, sort_keys=True), name="banking_transfer_workflow")

    events: list[dict[str, Any]] = []
    steering_history: list[str] = []
    final_answer: str
    blocked_output: str | None = None
    skip_luna_control = _should_skip_luna_control(argparse.Namespace(**vars(args)))

    try:
        if skip_luna_control:
            draft_response = _draft_transfer_plan_impl(prompt, transfer)
            events.append({"stage": "llm", "rows": [{"control": "Luna LLM control skipped on dev cluster"}]})
        else:
            try:
                draft_response = await draft_transfer_plan(prompt, transfer)
                events.append({"stage": "llm", "rows": []})
            except ControlViolationError as exc:
                blocked_output = _control_exception_message("llm", exc)
                events.append({"stage": "llm", "rows": _control_exception_rows(exc, "deny")})

        if blocked_output is None:
            logger.add_llm_span(
                input=prompt,
                output=draft_response,
                model="demo-rule-based",
                name=draft_transfer_plan.__name__,
                metadata={
                    "amount": transfer["amount"],
                    "destination_country": transfer["destination_country"],
                    "recipient_name": transfer["recipient_name"],
                },
            )

            tool_input = transfer
            for attempt in range(1, args.max_steer_attempts + 1):
                try:
                    tool_output = await process_wire_transfer(**tool_input)
                    events.append({"stage": f"tool attempt {attempt}", "input": tool_input, "rows": []})
                except ControlSteerError as exc:
                    events.append(
                        {
                            "stage": f"tool attempt {attempt}",
                            "input": tool_input,
                            "rows": _control_exception_rows(exc, "steer"),
                        }
                    )
                    steering_history.append(_describe_steering(exc))
                    tool_input = _apply_steering_context(tool_input, exc)
                    continue
                except ControlViolationError as exc:
                    blocked_output = _control_exception_message(f"tool attempt {attempt}", exc)
                    events.append(
                        {
                            "stage": f"tool attempt {attempt}",
                            "input": tool_input,
                            "rows": _control_exception_rows(exc, "deny"),
                        }
                    )
                    break
                logger.add_tool_span(
                    input=json.dumps(tool_input, sort_keys=True),
                    output=json.dumps(tool_output, sort_keys=True),
                    name=process_wire_transfer.__name__,
                    metadata={"steering_attempts": len(steering_history)},
                )
                break
            else:
                blocked_output = f"Execution blocked: steering did not converge within {args.max_steer_attempts} attempts."

        if blocked_output is None:
            final_answer = _render_final_answer(draft_response, tool_output, steering_history)
            status = "completed"
        else:
            final_answer = blocked_output
            status = "blocked"

        logger.conclude(output=final_answer)
        logger.flush()

        control_spans = [span for span in getattr(workflow, "spans", []) if getattr(span, "type", None) == "control"]
        trace_id = str(trace.id)
        persisted_records = await _readback_trace_records(
            args,
            project_id=logger.project_id,
            log_stream_id=logger.log_stream_id,
            trace_id=trace_id,
        )
        persisted_control_records = [
            record for record in persisted_records if record.get("type") == "control"
        ]
        return {
            "status": status,
            "answer": final_answer,
            "events": events,
            "initial_transfer": transfer,
            "final_transfer": locals().get("tool_input", transfer),
            "steering_history": steering_history,
            "project_id": logger.project_id,
            "log_stream_id": logger.log_stream_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "control_span_count": len(control_spans),
            "control_span_names": [str(span.name) for span in control_spans],
            "persisted_control_span_count": len(persisted_control_records),
            "persisted_control_names": sorted(
                {
                    str(record["name"])
                    for record in persisted_control_records
                    if record.get("name")
                }
            ),
            "target_type": target_type,
            "target_id": target_id,
        }
    finally:
        logger.terminate()


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def main() -> None:
    st.set_page_config(page_title="Banking Transfer Controls", layout="wide")
    st.title("Banking Transfer Controls")

    args = _defaults()
    with st.sidebar:
        st.header("Configuration")
        args.server_url = st.text_input("Agent Control URL", args.server_url)
        args.agent_name = st.text_input("Agent name", args.agent_name)
        st.caption(f"GALILEO_PROJECT: {args.project}")
        st.caption(f"GALILEO_LOG_STREAM: {args.log_stream}")
        st.caption(f"GALILEO_API_URL: {args.api_base_url}")
        st.caption(f"GALILEO_CONSOLE_URL: {args.console_url}")
        st.caption(f"GALILEO_API_KEY: {_masked_api_key()}")
        st.caption(f"AGENT_CONTROL_TARGET_TYPE: {args.target_type}")
        st.caption("Controls are loaded from the controls already attached in Console.")
        st.divider()
        st.header("Controls")
        for name, spec in control_specs():
            action = spec.get("action", {}).get("decision")
            st.caption(f"{name} - {action}")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected = st.selectbox("Scenario", list(STREAMLIT_SCENARIOS))
        scenario = STREAMLIT_SCENARIOS[selected]
        st.caption(f"Expected: {scenario['expectation']}")
        prompt = st.text_area(
            "Transfer request",
            value=scenario["prompt"],
            height=120,
            key=f"prompt::{selected}",
        )
    with col_b:
        amount = st.number_input(
            "Amount override",
            min_value=0.0,
            value=scenario["amount"],
            step=1000.0,
            key=f"amount::{selected}",
        )
        destination = st.text_input(
            "Destination override",
            value=scenario["destination"],
            key=f"destination::{selected}",
        )
        recipient = st.text_input(
            "Recipient override",
            value=scenario["recipient"],
            key=f"recipient::{selected}",
        )
        fraud = st.slider(
            "Fraud score override",
            min_value=0.0,
            max_value=1.0,
            value=scenario["fraud_score"],
            step=0.05,
            key=f"fraud::{selected}",
        )

    run_clicked = st.button("Run transfer", type="primary", width="stretch")
    if not run_clicked:
        return

    if not os.environ.get("GALILEO_API_KEY"):
        st.error("Set GALILEO_API_KEY in the environment before running the interactive app.")
        return

    with st.spinner("Running Agent Control checks and emitting Galileo spans..."):
        try:
            result = _run_async(
                _run_banking_agent(
                    args=args,
                    prompt=prompt,
                    amount=amount or None,
                    destination_country=destination or None,
                    recipient_name=recipient or None,
                    fraud_score=fraud if fraud > 0 else None,
                )
            )
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
            return

    status = result["status"]
    if status == "completed":
        st.success("Transfer completed after controls passed.")
    else:
        st.error("Transfer blocked by a deny control.")

    scenario_failures = _scenario_failures(scenario, result)
    if scenario_failures:
        st.error("Scenario validation failed: " + "; ".join(scenario_failures))
    else:
        st.success(f"Scenario validation passed: {scenario['expectation']}")

    st.subheader("Agent Response")
    st.code(result["answer"], language="text")

    metrics = st.columns(4)
    metrics[0].metric("Control spans", result["control_span_count"])
    metrics[1].metric("Steers", len(result["steering_history"]))
    metrics[2].metric("Project", result["project_id"][:8])
    metrics[3].metric("Trace", result["trace_id"][:8])
    st.caption(f"Agent Control target: {result['target_type']}:{result['target_id']}")

    if result["steering_history"]:
        st.subheader("Steering Applied")
        for item in result["steering_history"]:
            st.info(item)

    st.subheader("Transfer State")
    before, after = st.columns(2)
    before.json(result["initial_transfer"])
    after.json(result["final_transfer"])

    st.subheader("Control Evaluations")
    for event in result["events"]:
        with st.expander(event["stage"], expanded=True):
            if "input" in event:
                st.caption("Evaluated input")
                st.json(event["input"])
            rows = event["rows"]
            if rows:
                st.dataframe(rows, width="stretch", hide_index=True)
            else:
                st.caption("No matched controls or errors.")


if __name__ == "__main__":
    main()
