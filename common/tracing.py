"""OpenTelemetry tracing setup shared by all services.

Call setup_tracing(service_name) once in service lifespan before handling requests.
Use get_tracer(name) anywhere for manual spans.
Use inject/extract helpers for W3C context propagation over NATS.
"""
import os

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import SERVICE_INSTANCE_ID, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(service_name: str) -> None:
    """Configure OTel SDK with OTLP/gRPC export. Call once at service startup."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    instance_id = os.getenv("INSTANCE_ID", service_name)

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_INSTANCE_ID: instance_id,
    })
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    RedisInstrumentor().instrument()


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def inject_trace_context(payload: dict) -> dict:
    """Inject W3C traceparent/tracestate into a NATS payload for cross-service propagation."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    if carrier:
        return {**payload, "_trace": carrier}
    return payload


def extract_trace_context(payload: dict):
    """Extract OTel context from a NATS payload. Returns a Context for span creation."""
    return propagate.extract(payload.get("_trace") or {})
