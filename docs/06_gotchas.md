# The four wiring gotchas

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
