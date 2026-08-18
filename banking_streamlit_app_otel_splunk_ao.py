#!/usr/bin/env python3
"""Interactive banking demo that exports application and control spans via OTLP/HTTP."""

from __future__ import annotations

# ruff: noqa: I001 -- load the selected dotenv file before app imports

import argparse
import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv(".env.splunk-ao", override=True)

from banking_streamlit_app_splunk_ao import STREAMLIT_EXAMPLES, _masked_api_key
from common_splunk_ao import (
    DEFAULT_AGENT_CONTROL_URL,
    DEFAULT_AGENT_NAME,
    DEFAULT_API_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_LOG_STREAM,
    DEFAULT_PROJECT,
    resolve_agent_control_api_key,
    resolve_agent_control_api_key_header,
)

from agent_control import ControlSteerError, ControlViolationError

from run_demo_otel_splunk_ao import (
    _otel_endpoint,
    _otel_headers,
    _otel_trace_context_for_span,
    _resolve_splunk_ao_ids,
    _set_common_span_attributes,
    _trace_uuid,
    _validate_otel_configuration,
)
from run_demo_splunk_ao import (
    _apply_steering_context,
    _compute_fraud_score,
    _control_exception_message,
    _control_exception_rows,
    _describe_steering,
    _draft_transfer_plan_impl,
    _login_with_api_key,
    _parse_transfer_request,
    _render_final_answer,
    _should_skip_luna_control,
    _verify_bound_controls,
    draft_transfer_plan,
    process_wire_transfer,
)
from setup_controls_splunk_ao import control_specs


def _defaults() -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=os.environ.get("AGENT_CONTROL_AGENT_NAME", DEFAULT_AGENT_NAME),
        server_url=os.environ.get("AGENT_CONTROL_URL", DEFAULT_AGENT_CONTROL_URL),
        project=os.environ.get("SPLUNK_AO_PROJECT", DEFAULT_PROJECT),
        log_stream=os.environ.get("SPLUNK_AO_AGENT_STREAM", DEFAULT_LOG_STREAM),
        console_url=os.environ.get("SPLUNK_AO_CONSOLE_URL", DEFAULT_CONSOLE_URL),
        api_base_url=os.environ.get("SPLUNK_AO_API_URL", DEFAULT_API_URL),
        runtime_auth_mode=os.environ.get("AGENT_CONTROL_RUNTIME_AUTH_MODE", "jwt"),
        target_type=os.environ.get("AGENT_CONTROL_TARGET_TYPE", "log_stream"),
        skip_runtime_token_check=True,
        skip_scorer_invoke_check=True,
        skip_luna_control=None,
        expect_luna_deny=False,
        max_steer_attempts=3,
    )


def _configure_environment(args: SimpleNamespace) -> None:
    os.environ["SPLUNK_AO_CONSOLE_URL"] = args.console_url
    os.environ["SPLUNK_AO_API_URL"] = args.api_base_url
    os.environ["SPLUNK_AO_PROJECT"] = args.project
    os.environ["SPLUNK_AO_AGENT_STREAM"] = args.log_stream
    os.environ["AGENT_CONTROL_URL"] = args.server_url
    os.environ["AGENT_CONTROL_RUNTIME_AUTH_MODE"] = args.runtime_auth_mode
    os.environ["AGENT_CONTROL_OBSERVABILITY_SINK_NAME"] = "otel"
    os.environ["AGENT_CONTROL_OTEL_ENABLED"] = "true"
    resolve_agent_control_api_key()
    resolve_agent_control_api_key_header()


async def _readback_counts(
    args: SimpleNamespace,
    project_id: str,
    agent_stream_id: str,
    trace_id: str,
) -> tuple[int, int]:
    api_key = os.environ.get("SPLUNK_AO_API_KEY")
    if not api_key:
        return 0, 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        bearer_headers = await _login_with_api_key(
            client, args.api_base_url.rstrip("/"), api_key
        )
        for attempt in range(10):
            try:
                response = await client.post(
                    f"{args.api_base_url.rstrip('/')}/projects/{project_id}/spans/partial_search",
                    headers=bearer_headers,
                    json={
                        "log_stream_id": agent_stream_id,
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
                            "column_ids": ["id", "trace_id", "type", "name"],
                            "include_all_metrics": False,
                            "include_all_feedback": False,
                        },
                    },
                )
                if response.status_code == 200:
                    records = response.json().get("records", [])
                    unique_records = {
                        str(record.get("id")): record for record in records
                    }
                    typed_count = sum(
                        record.get("type") == "control"
                        for record in unique_records.values()
                    )
                    raw_count = sum(
                        record.get("name") == "agent_control.control_execution"
                        or record.get("type") == "control"
                        for record in unique_records.values()
                    )
                    if raw_count or typed_count or attempt == 9:
                        return raw_count, typed_count
                else:
                    response.raise_for_status()
            except httpx.HTTPError:
                if attempt == 9:
                    return 0, 0
            await asyncio.sleep(1.0)
    return 0, 0


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
    endpoint = _otel_endpoint(args)
    headers = _otel_headers(args)
    _validate_otel_configuration(args, endpoint, headers)

    import agent_control
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from splunk_ao import otel

    project_id, log_stream_id = await asyncio.to_thread(_resolve_splunk_ao_ids, args)

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": args.agent_name,
            }
        )
    )
    otel.add_splunk_ao_span_processor(
        provider,
        project=args.project,
        agentstream=args.log_stream,
    )
    tracer = provider.get_tracer("agent-control-banking-streamlit-otel")

    agent_control.init(
        agent_name=args.agent_name,
        agent_description="Interactive banking transfer demo using OTLP application and control spans",
        server_url=args.server_url,
        api_key=resolve_agent_control_api_key(),
        api_key_header=resolve_agent_control_api_key_header(),
        observability_enabled=True,
        observability_sink_name="otel",
        observability_sink_config={
            "enabled": True,
            "endpoint": endpoint,
            "headers": headers,
            "service_name": args.agent_name,
        },
        target_type=args.target_type,
        target_id=log_stream_id,
    )

    control_args = argparse.Namespace(**vars(args))
    await _verify_bound_controls(
        control_args, target_type=args.target_type, target_id=log_stream_id
    )

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
    events: list[dict[str, Any]] = []
    steering_history: list[str] = []
    blocked_output: str | None = None
    status = "blocked"
    root_span = None

    try:
        with tracer.start_as_current_span(
            "agent-control-banking-streamlit-otel"
        ) as root_span:
            _set_common_span_attributes(
                root_span, operation="invoke_workflow", input_value=trace_input
            )
            root_span.set_attribute("splunk_ao.demo.transport", "otlp_http")
            workflow_trace_context = _otel_trace_context_for_span(root_span)
            agent_control.set_trace_context_provider(lambda: workflow_trace_context)

            skip_luna_control = _should_skip_luna_control(
                argparse.Namespace(**vars(args))
            )
            if skip_luna_control:
                draft_response = _draft_transfer_plan_impl(prompt, transfer)
                events.append(
                    {"stage": "llm", "rows": [{"control": "Luna LLM control skipped"}]}
                )
            else:
                with tracer.start_as_current_span(
                    draft_transfer_plan.__name__
                ) as llm_span:
                    _set_common_span_attributes(
                        llm_span, operation="chat", input_value=prompt
                    )
                    llm_span.set_attribute("gen_ai.request.model", "demo-rule-based")
                    try:
                        draft_response = await draft_transfer_plan(prompt, transfer)
                        events.append({"stage": "llm", "rows": []})
                    except ControlViolationError as exc:
                        blocked_output = _control_exception_message("llm", exc)
                        events.append(
                            {
                                "stage": "llm",
                                "rows": _control_exception_rows(exc, "deny"),
                            }
                        )
                    else:
                        llm_span.set_attribute(
                            "gen_ai.output.messages",
                            json.dumps(
                                [{"role": "assistant", "content": draft_response}]
                            ),
                        )

            tool_input = transfer
            tool_output: dict[str, Any] = {}
            if blocked_output is None:
                for attempt in range(1, args.max_steer_attempts + 1):
                    with tracer.start_as_current_span(
                        process_wire_transfer.__name__
                    ) as tool_span:
                        _set_common_span_attributes(
                            tool_span, operation="execute_tool", input_value=tool_input
                        )
                        tool_span.set_attribute(
                            "gen_ai.tool.name", process_wire_transfer.__name__
                        )
                        tool_span.set_attribute(
                            "gen_ai.tool.call.arguments",
                            json.dumps(tool_input, sort_keys=True),
                        )
                        try:
                            tool_output = await process_wire_transfer(**tool_input)
                            events.append(
                                {
                                    "stage": f"tool attempt {attempt}",
                                    "input": tool_input,
                                    "rows": [],
                                }
                            )
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
                            tool_span.set_attribute("agent_control.retry", True)
                            continue
                        except ControlViolationError as exc:
                            blocked_output = _control_exception_message(
                                f"tool attempt {attempt}", exc
                            )
                            events.append(
                                {
                                    "stage": f"tool attempt {attempt}",
                                    "input": tool_input,
                                    "rows": _control_exception_rows(exc, "deny"),
                                }
                            )
                            break
                        tool_span.set_attribute(
                            "gen_ai.tool.call.result",
                            json.dumps(tool_output, sort_keys=True),
                        )
                        tool_span.set_attribute(
                            "gen_ai.output.messages",
                            json.dumps(
                                [{"role": "tool", "content": tool_output}], default=str
                            ),
                        )
                        break
                else:
                    blocked_output = f"Execution blocked: steering did not converge within {args.max_steer_attempts} attempts."

            if blocked_output is None:
                final_answer = _render_final_answer(
                    draft_response, tool_output, steering_history
                )
                status = "completed"
            else:
                final_answer = blocked_output

            root_span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": final_answer}]),
            )

        trace_id = _trace_uuid(root_span)
    finally:
        agent_control.clear_trace_context_provider()
        await agent_control.shutdown_observability()
        provider.force_flush()
        provider.shutdown()

    raw_control_events, typed_control_spans = await _readback_counts(
        args,
        project_id,
        log_stream_id,
        trace_id,
    )
    return {
        "status": status,
        "answer": final_answer,
        "events": events,
        "initial_transfer": transfer,
        "final_transfer": tool_input,
        "steering_history": steering_history,
        "project_id": project_id,
        "agent_stream_id": log_stream_id,
        "trace_id": trace_id,
        "raw_control_events": raw_control_events,
        "typed_control_spans": typed_control_spans,
        "target_type": args.target_type,
        "target_id": log_stream_id,
        "otel_endpoint": endpoint,
    }


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def main() -> None:
    st.set_page_config(page_title="Banking Transfer Controls — OTEL", layout="wide")
    st.title("Banking Transfer Controls — OTEL")
    st.caption(
        "Application traces and Agent Control events are exported through OTLP/HTTP; SplunkAOLogger is not used."
    )

    args = _defaults()
    with st.sidebar:
        st.header("Configuration")
        args.server_url = st.text_input("Agent Control URL", args.server_url)
        args.agent_name = st.text_input("Agent name", args.agent_name)
        st.caption(f"SPLUNK_AO_PROJECT: {args.project}")
        st.caption(f"SPLUNK_AO_AGENT_STREAM: {args.log_stream}")
        st.caption(f"SPLUNK_AO_API_URL: {args.api_base_url}")
        st.caption(f"SPLUNK_AO_API_KEY: {_masked_api_key()}")
        st.caption(
            f"OTLP endpoint: {os.environ.get('AGENT_CONTROL_OTEL_ENDPOINT', 'derived from API URL')}"
        )
        st.caption("Agent Control sink: otel")
        st.divider()
        st.header("Controls")
        for name, spec in control_specs():
            st.caption(f"{name} — {spec.get('action', {}).get('decision')}")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected = st.selectbox("Scenario", list(STREAMLIT_EXAMPLES))
        prompt = st.text_area(
            "Transfer request", value=STREAMLIT_EXAMPLES[selected], height=120
        )
    with col_b:
        amount = st.number_input(
            "Amount override", min_value=0.0, value=0.0, step=1000.0
        )
        destination = st.text_input("Destination override", value="")
        recipient = st.text_input("Recipient override", value="")
        fraud = st.slider("Fraud score override", 0.0, 1.0, 0.0, 0.05)

    if not st.button("Run transfer via OTEL", type="primary", width="stretch"):
        return
    if not os.environ.get("SPLUNK_AO_API_KEY"):
        st.error("Set SPLUNK_AO_API_KEY before starting Streamlit.")
        return

    with st.spinner("Running controls, exporting OTLP spans, and checking readback..."):
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
        except Exception as exc:  # noqa: BLE001 - surface demo failures in the UI
            st.error(f"{type(exc).__name__}: {exc}")
            return

    if result["status"] == "completed":
        st.success("Transfer completed after controls passed.")
    else:
        st.error("Transfer blocked by a deny control.")

    st.subheader("Agent Response")
    st.code(result["answer"], language="text")
    metrics = st.columns(4)
    metrics[0].metric("OTEL control events", result["raw_control_events"])
    metrics[1].metric("Typed control spans", result["typed_control_spans"])
    metrics[2].metric("Steers", len(result["steering_history"]))
    metrics[3].metric("Trace", result["trace_id"][:8])

    if result["raw_control_events"] and not result["typed_control_spans"]:
        st.warning(
            "Agent Control OTEL events reached Splunk AO but were not normalized as typed control spans. "
            "Deploy an ingest-service build containing Orbit commit 9ccc212c."
        )

    st.caption(f"OTLP endpoint: {result['otel_endpoint']}")
    st.caption(f"Agent Control target: {result['target_type']}:{result['target_id']}")
    st.caption(
        f"Splunk AO project/Agent Stream IDs: {result['project_id']} / {result['agent_stream_id']}"
    )

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
            if event["rows"]:
                st.dataframe(event["rows"], width="stretch", hide_index=True)
            else:
                st.caption("No matched controls or errors.")


if __name__ == "__main__":
    main()
