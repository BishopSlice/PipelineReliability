from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def setup_tracing():
    """
    Configure OpenTelemetry with Google Cloud Trace exporter.
    Returns the TracerProvider (call provider.force_flush() on exit).
    Returns None if GOOGLE_CLOUD_PROJECT is not set (tracing disabled).
    """
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")

    if not project_id:
        logger.info(
            "GOOGLE_CLOUD_PROJECT not set — Cloud Trace disabled. "
            "Set this env var to enable trace observability in GCP Console."
        )
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace import export as sdk_export

        provider = TracerProvider()
        processor = sdk_export.BatchSpanProcessor(
            CloudTraceSpanExporter(project_id=project_id)
        )
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

        logger.info("Cloud Trace enabled — traces will appear in project: %s", project_id)
        print(f"  Tracing → https://console.cloud.google.com/traces/list?project={project_id}")

        return provider

    except ImportError:
        logger.warning(
            "opentelemetry-exporter-gcp-trace not installed. "
            "Install with: pip install opentelemetry-exporter-gcp-trace"
        )
        return None
    except Exception as e:
        logger.warning("Failed to set up Cloud Trace: %s", str(e))
        return None
