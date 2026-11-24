"""OpenTelemetry setup.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so a laptop run has no extra
moving parts. When on, every service exports spans over OTLP/HTTP and outgoing
httpx calls (MCP, A2A, token requests) carry a W3C `traceparent` header, which
is what stitches one run's trace across services in Jaeger or Tempo.

LangSmith tracing is separate and needs no code: LangChain picks up
LANGSMITH_TRACING / LANGSMITH_API_KEY from the environment.
"""

from __future__ import annotations

from mara_core.logging import get_logger

log = get_logger(__name__)
_done = False


def setup_telemetry(service_name: str, endpoint: str | None) -> None:
    global _done
    if _done or not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _done = True
    log.info("telemetry_enabled", endpoint=endpoint)


def instrument_fastapi(app) -> None:
    if not _done:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # optional dependency
        return
    FastAPIInstrumentor.instrument_app(app)


def tracer(name: str):
    from opentelemetry import trace

    return trace.get_tracer(name)
