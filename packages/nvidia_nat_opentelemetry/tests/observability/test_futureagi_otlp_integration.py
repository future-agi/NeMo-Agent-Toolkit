# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Exercise Future AGI trace export against local HTTP and gRPC collectors only."""

import logging
import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import grpc
import pytest
import pytest_httpserver
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from werkzeug import Response

from nat.builder.framework_enum import LLMFrameworkEnum
from nat.data_models.intermediate_step import IntermediateStep
from nat.data_models.intermediate_step import IntermediateStepPayload
from nat.data_models.intermediate_step import IntermediateStepType
from nat.data_models.intermediate_step import StreamEventData
from nat.data_models.invocation_node import InvocationNode
from nat.plugins.opentelemetry.register import FutureAGITelemetryExporter
from nat.plugins.opentelemetry.register import futureagi_telemetry_exporter

pytestmark = pytest.mark.timeout(15)
MARKER = "MARKER9b2c"
OTEL_LOGGERS = {
    "http": "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "grpc": "opentelemetry.exporter.otlp.proto.grpc.exporter",
}


@pytest.fixture(autouse=True)
def isolated_export_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(("OTEL_EXPORTER_OTLP", "FI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")


@pytest.fixture(params=["http", "grpc"])
def receiver(request):
    state = SimpleNamespace(protocol=request.param, requests=[], headers=[], reject=False)
    if state.protocol == "http":
        server = pytest_httpserver.HTTPServer(host="127.0.0.1", port=0)

        def handle_trace(request):
            state.requests.append(trace_service_pb2.ExportTraceServiceRequest.FromString(request.data))
            state.headers.append(dict(request.headers))
            if state.reject:
                return Response(status=401, response="Unauthorized")
            return Response(trace_service_pb2.ExportTraceServiceResponse().SerializeToString(),
                            content_type="application/x-protobuf")

        server.expect_request("/tracer/v1/traces", method="POST").respond_with_handler(handle_trace)
        server.start()
        state.endpoint = f"http://127.0.0.1:{server.port}/tracer/v1/traces"
        try:
            yield state
        finally:
            server.stop()
    else:

        class TraceService(trace_service_pb2_grpc.TraceServiceServicer):

            def Export(self, request, context):
                state.requests.append(request)
                state.headers.append(dict(context.invocation_metadata()))
                if state.reject:
                    context.abort(grpc.StatusCode.UNAUTHENTICATED, "Unauthorized")
                return trace_service_pb2.ExportTraceServiceResponse()

        with ThreadPoolExecutor(max_workers=2) as pool:
            server = grpc.server(pool)
            trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(), server)
            port = server.add_insecure_port("127.0.0.1:0")
            server.start()
            state.endpoint = f"http://127.0.0.1:{port}"
            try:
                yield state
            finally:
                server.stop(0).wait(timeout=5)


@asynccontextmanager
async def configured_exporter(endpoint, protocol, **overrides):
    values = {
        "project": "local-project",
        "api_key": f"api-{MARKER}",
        "secret_key": f"secret-{MARKER}",
        "endpoint": endpoint,
        "protocol": protocol,
        "batch_size": 1,
        "flush_interval": 60.0,
    }
    values.update(overrides)
    config = FutureAGITelemetryExporter(**values)
    async with futureagi_telemetry_exporter(config, builder=MagicMock()) as exporter:
        try:
            yield exporter
        finally:
            exporter._exporter.shutdown()


def export_span(exporter, name):
    """Drive the same START/END event pipeline as the existing OTLP integration tests."""
    span_uuid = str(uuid.uuid4())
    for event_type, data in [(IntermediateStepType.LLM_START, StreamEventData(input="test input")),
                             (IntermediateStepType.LLM_END, StreamEventData(output="test output"))]:
        exporter.export(
            IntermediateStep(parent_id="root",
                             function_ancestry=InvocationNode(function_name=name, function_id=span_uuid,
                                                              parent_id=None),
                             payload=IntermediateStepPayload(UUID=span_uuid,
                                                             event_type=event_type,
                                                             framework=LLMFrameworkEnum.LANGCHAIN,
                                                             name=name,
                                                             event_timestamp=time.time(),
                                                             data=data)))


def received_names(receiver):
    return [
        span.name for request in receiver.requests for resource in request.resource_spans
        for scope in resource.scope_spans for span in scope.spans
    ]


def assert_routing(receiver, service_name="local-project"):
    assert receiver.requests
    for request in receiver.requests:
        assert request.resource_spans
        for resource in request.resource_spans:
            attributes = {entry.key: entry.value.string_value for entry in resource.resource.attributes}
            assert attributes == {
                "project_name": "local-project",
                "project_type": "observe",
                "service.name": service_name,
            }


@pytest.mark.parametrize("service_name", ["local-project", "custom-service"])
async def test_trace_resources_and_authentication_on_wire(receiver, service_name, caplog):
    caplog.set_level(logging.DEBUG)
    async with configured_exporter(receiver.endpoint,
                                   receiver.protocol,
                                   resource_attributes={"service.name": service_name}) as exporter:
        async with exporter.start():
            for name in ("first", "second"):
                export_span(exporter, name)
                await exporter.wait_for_tasks()
    assert received_names(receiver) == ["first", "second"]
    assert_routing(receiver, service_name)
    for headers in receiver.headers:
        if receiver.protocol == "http":
            assert headers["X-Api-Key"] == f"api-{MARKER}"
            assert headers["X-Secret-Key"] == f"secret-{MARKER}"
            assert headers["Content-Type"] == "application/x-protobuf"
        else:
            assert headers["x-api-key"] == f"api-{MARKER}"
            assert headers["x-secret-key"] == f"secret-{MARKER}"
            assert all(key == key.lower() for key in headers)
    assert MARKER not in caplog.text


async def test_authentication_failure_logs_error_and_exporter_recovers(receiver, caplog):
    caplog.set_level(logging.DEBUG)
    receiver.reject = True
    async with configured_exporter(receiver.endpoint, receiver.protocol) as exporter:
        async with exporter.start():
            export_span(exporter, "rejected")
            await exporter.wait_for_tasks()
            errors = [
                record for record in caplog.records
                if record.levelno == logging.ERROR and record.name == OTEL_LOGGERS[receiver.protocol]
            ]
            assert errors
            expected_status = "401" if receiver.protocol == "http" else "UNAUTHENTICATED"
            assert any(expected_status in record.getMessage() for record in errors)
            assert MARKER not in caplog.text
            caplog.clear()
            receiver.reject = False
            export_span(exporter, "recovered")
            await exporter.wait_for_tasks()
            assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert received_names(receiver) == ["rejected", "recovered"]
    assert_routing(receiver)
    assert MARKER not in caplog.text


@pytest.mark.parametrize("protocol", ["http", "grpc"])
@pytest.mark.timeout(10)
async def test_unreachable_collector_is_bounded_and_does_not_raise(protocol, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    # Reserve an unused local port without listening, preventing another test from taking it.
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        port = closed_port.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}"
        if protocol == "http":
            endpoint += "/tracer/v1/traces"
        started = time.monotonic()
        async with configured_exporter(endpoint, protocol) as exporter:
            async with exporter.start():
                export_span(exporter, "unreachable")
                await exporter.wait_for_tasks()
        assert time.monotonic() - started < 5
    # HTTP: connection error propagates to the NAT mixin logger. gRPC: the installed exporter (1.39.1) retries
    # UNAVAILABLE, then logs an ERROR from its own logger once retries are exhausted or the deadline passes.
    expected_level = logging.ERROR
    expected_logger = ("nat.plugins.opentelemetry.mixin.otlp_span_exporter_mixin"
                       if protocol == "http" else OTEL_LOGGERS[protocol])
    assert any(record.levelno == expected_level and record.name == expected_logger for record in caplog.records)
    assert MARKER not in caplog.text


async def test_shutdown_flushes_remaining_spans(receiver):
    async with configured_exporter(receiver.endpoint, receiver.protocol, batch_size=10) as exporter:
        async with exporter.start():
            export_span(exporter, "pending")
            await exporter.wait_for_tasks()
            assert receiver.requests == []
        assert received_names(receiver) == ["pending"]
    assert_routing(receiver)


async def test_batch_size_triggers_export(receiver):
    async with configured_exporter(receiver.endpoint, receiver.protocol, batch_size=2) as exporter:
        async with exporter.start():
            export_span(exporter, "first")
            await exporter.wait_for_tasks()
            assert receiver.requests == []
            export_span(exporter, "second")
            await exporter.wait_for_tasks()
            assert received_names(receiver) == ["first", "second"]
            assert len(receiver.requests) == 1
    assert_routing(receiver)


@pytest.mark.parametrize("drop_on_overflow", [True, False])
async def test_queue_overflow_drops_or_forces_flush(receiver, drop_on_overflow, caplog):
    caplog.set_level(logging.WARNING)
    async with configured_exporter(receiver.endpoint,
                                   receiver.protocol,
                                   batch_size=10,
                                   max_queue_size=2,
                                   drop_on_overflow=drop_on_overflow) as exporter:
        async with exporter.start():
            for name in ("first", "second", "overflow"):
                export_span(exporter, name)
                await exporter.wait_for_tasks()
            if drop_on_overflow:
                assert receiver.requests == []
                assert "Dropping item due to queue overflow" in caplog.text
            else:
                assert received_names(receiver) == ["first", "second"]
                assert "Queue overflow, forcing flush" in caplog.text
        expected = ["first", "second"] if drop_on_overflow else ["first", "second", "overflow"]
        assert received_names(receiver) == expected
    assert_routing(receiver)
    assert MARKER not in caplog.text
