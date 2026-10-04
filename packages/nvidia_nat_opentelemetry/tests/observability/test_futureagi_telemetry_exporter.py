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
"""Future AGI configuration, registration, and secret-safe factory contracts."""

import logging
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import nat.plugins.opentelemetry.register as otel_register
from nat.cli.type_registry import GlobalTypeRegistry
from nat.plugins.opentelemetry.futureagi import RESERVED_RESOURCE_KEYS
from nat.plugins.opentelemetry.futureagi import build_auth_headers
from nat.plugins.opentelemetry.futureagi import build_resource_attributes
from nat.plugins.opentelemetry.futureagi import derive_http_endpoint
from nat.plugins.opentelemetry.futureagi import is_reserved_resource_key
from nat.plugins.opentelemetry.futureagi import resolve_credential
from nat.plugins.opentelemetry.futureagi import resolve_endpoint
from nat.plugins.opentelemetry.futureagi import resolve_project
from nat.plugins.opentelemetry.futureagi import validate_endpoint

MARKER = "MARKER7f3a"
FIELDS = [("project", "FI_PROJECT_NAME"), ("api_key", "FI_API_KEY"), ("secret_key", "FI_SECRET_KEY")]
BAD_ENDPOINTS = [
    (f"https://[{MARKER}00", "valid URL"),
    (f"https://{MARKER}01\uff0fhost", "valid URL"),
    (f"ftp://{MARKER}02.example", "scheme"),
    (f"https:///{MARKER}03", "hostname"),
    (f"https://host:{MARKER}04", "port"),
    (f"https://{MARKER}05.example:65536", "port"),
    (f"https://{MARKER}06.example:-1", "port"),
    (f"https://{MARKER}07@host", "credentials"),
    (f"https://user:{MARKER}08@host", "credentials"),
    (f"https://@{MARKER}09.example", "credentials"),
    (f"https://host/?token={MARKER}0a", "query or fragment"),
    (f"https://host/#{MARKER}0b", "query or fragment"),
    (f"https://{MARKER}0c.example/?", "query or fragment"),
    (f"https://{MARKER}0d.example/#", "query or fragment"),
    (f" https://{MARKER}0e.example", "whitespace or control"),
    (f"https://{MARKER}0f.example ", "whitespace or control"),
    (f"https://{MARKER}10.example/a b", "whitespace or control"),
    (f"https://{MARKER}11.example/\t", "whitespace or control"),
    (f"https://{MARKER}12.example/\n", "whitespace or control"),
    (f"https://{MARKER}13.example/\r", "whitespace or control"),
    (f"https://{MARKER}14.example/\x00", "whitespace or control"),
    (f"https://{MARKER}15.example/\x7f", "whitespace or control"),
    (f"https://{MARKER}16.example/\x80", "whitespace or control"),
    (f"https://{MARKER}17.example/\u00a0", "whitespace or control"),
]


@pytest.fixture(autouse=True)
def clean_futureagi_environment(monkeypatch):
    for name in ("FI_PROJECT_NAME", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_GRPC_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def recording_exporter(monkeypatch):
    instances = []

    class RecordingExporter:

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            instances.append(self)

    monkeypatch.setattr("nat.plugins.opentelemetry.OTLPSpanAdapterExporter", RecordingExporter)
    return instances


async def create_exporter(**overrides):
    values = {"project": "project", "api_key": f"api-{MARKER}", "secret_key": f"secret-{MARKER}"}
    values.update(overrides)
    config = otel_register.FutureAGITelemetryExporter(**values)
    # Registration wraps the factory in asynccontextmanager; unwrap to drive its generator directly.
    agen = otel_register.futureagi_telemetry_exporter.__wrapped__(config, builder=MagicMock())
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


def test_defaults_and_registration():
    config = otel_register.FutureAGITelemetryExporter()
    assert config.protocol == "http"
    assert config.endpoint is None
    assert config.project == ""
    assert config.api_key.get_secret_value() == ""
    assert config.secret_key.get_secret_value() == ""
    assert config.project_type == "observe"
    assert config.resource_attributes == {}
    assert config.static_type() == "futureagi"
    registration = GlobalTypeRegistry.get().get_telemetry_exporter(type(config))
    assert registration.build_fn is otel_register.futureagi_telemetry_exporter


@pytest.mark.parametrize("protocol, expected", [("http", "https://api.futureagi.com/tracer/v1/traces"),
                                                ("grpc", "https://grpc.futureagi.com")])
async def test_hosted_defaults(protocol, expected, recording_exporter):
    assert resolve_endpoint(None, protocol, {}) == expected
    exporter = await create_exporter(protocol=protocol)
    assert exporter.kwargs["endpoint"] == expected
    assert exporter.kwargs["protocol"] == protocol


@pytest.mark.parametrize("base, expected",
                         [("https://h", "https://h/tracer/v1/traces"), ("https://h/", "https://h/tracer/v1/traces"),
                          ("https://h/foo/", "https://h/tracer/v1/traces"),
                          ("https://h:8443/x", "https://h:8443/tracer/v1/traces"),
                          ("http://h/foo", "http://h/tracer/v1/traces")])
def test_http_derivation(base, expected):
    assert derive_http_endpoint(base) == expected
    assert resolve_endpoint(None, "http", {"FI_BASE_URL": base}) == expected


@pytest.mark.parametrize("protocol, env_name, expected",
                         [
                             ("http", "FI_BASE_URL", "https://collector.example/tracer/v1/traces"),
                             ("grpc", "FI_GRPC_URL", "https://collector.example/custom/"),
                         ])
async def test_endpoint_environment_and_explicit_precedence(protocol,
                                                            env_name,
                                                            expected,
                                                            monkeypatch,
                                                            recording_exporter):
    monkeypatch.setenv(env_name, "https://collector.example/custom/")
    exporter = await create_exporter(protocol=protocol)
    assert exporter.kwargs["endpoint"] == expected
    # Explicit URLs retain their path and casing, even with an invalid environment URL.
    monkeypatch.setenv(env_name, f"invalid-{MARKER}")
    explicit = "https://Custom.example:8443/exact/path/"
    exporter = await create_exporter(protocol=protocol, endpoint=explicit)
    assert exporter.kwargs["endpoint"] == explicit


@pytest.mark.parametrize("protocol", ["http", "grpc"])
@pytest.mark.parametrize("url, rule", BAD_ENDPOINTS)
async def test_rejected_endpoints_never_echo_values(url, rule, protocol, recording_exporter, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match=rule) as exc:
        validate_endpoint(url, protocol)
    assert MARKER not in str(exc.value)
    with pytest.raises(ValueError, match=rule) as exc:
        await create_exporter(endpoint=url, protocol=protocol)
    assert MARKER not in str(exc.value)
    assert MARKER not in caplog.text
    assert recording_exporter == []


@pytest.mark.parametrize("url, rule", BAD_ENDPOINTS)
@pytest.mark.parametrize("protocol, env_name", [("http", "FI_BASE_URL"), ("grpc", "FI_GRPC_URL")])
async def test_environment_endpoints_are_validated(url, rule, protocol, env_name, monkeypatch, recording_exporter):
    # A mapping also covers NUL validation; operating systems cannot store NUL in environment variables.
    monkeypatch.setattr(otel_register.os, "environ", {env_name: url})
    with pytest.raises(ValueError, match=rule) as exc:
        await create_exporter(protocol=protocol)
    assert MARKER not in str(exc.value)
    assert recording_exporter == []


@pytest.mark.parametrize("endpoint", ["", " "])
async def test_explicit_blank_endpoint_is_rejected(endpoint, recording_exporter):
    with pytest.raises(ValueError, match="endpoint"):
        await create_exporter(endpoint=endpoint)
    assert recording_exporter == []


@pytest.mark.parametrize("field, env_name", FIELDS)
@pytest.mark.parametrize("config_value", [None, "", " \t\n "])
@pytest.mark.parametrize("env_value", [None, "", " \t\n "])
async def test_missing_required_values_fail_before_construction(field,
                                                                env_name,
                                                                config_value,
                                                                env_value,
                                                                monkeypatch,
                                                                recording_exporter,
                                                                caplog):
    caplog.set_level(logging.DEBUG)
    values = {name: f"{name}-{MARKER}" for name, _ in FIELDS}
    values.pop(field)
    if config_value is not None:
        values[field] = config_value
    if env_value is not None:
        monkeypatch.setenv(env_name, env_value)
    config = otel_register.FutureAGITelemetryExporter(**values)
    agen = otel_register.futureagi_telemetry_exporter.__wrapped__(config, builder=MagicMock())
    try:
        with pytest.raises(ValueError) as exc:
            await agen.__anext__()
        assert str(exc.value) == (f"{field} is required for futureagi (set `{field}` on the exporter or {env_name})")
        assert MARKER not in str(exc.value)
        assert MARKER not in caplog.text
        assert recording_exporter == []
    finally:
        await agen.aclose()


@pytest.mark.parametrize("field, env_name", FIELDS)
@pytest.mark.parametrize("config_value", ["", " \t ", " config-value "])
@pytest.mark.parametrize("env_value", [None, "", " \t ", " env-value "])
async def test_config_environment_precedence(field, env_name, config_value, env_value, monkeypatch, recording_exporter):
    env = {} if env_value is None else {env_name: env_value}
    if env_value is not None:
        monkeypatch.setenv(env_name, env_value)
    expected = config_value.strip() or (env_value or "").strip()
    if not expected:
        with pytest.raises(ValueError):
            await create_exporter(**{field: config_value})
        assert recording_exporter == []
        return
    if field == "project":
        assert resolve_project(config_value, env) == expected
    else:
        assert resolve_credential(config_value, env_name, field, env) == expected
    exporter = await create_exporter(**{field: config_value})
    actual = (exporter.kwargs["resource_attributes"]["project_name"] if field == "project" else
              exporter.kwargs["headers"]["X-Api-Key" if field == "api_key" else "X-Secret-Key"])
    assert actual == expected


@pytest.mark.parametrize("protocol, headers",
                         [("http", {
                             "X-Api-Key": "api", "X-Secret-Key": "secret"
                         }), ("grpc", {
                             "x-api-key": "api", "x-secret-key": "secret"
                         })])
async def test_auth_header_casing(protocol, headers, recording_exporter):
    assert build_auth_headers("api", "secret", protocol) == headers
    exporter = await create_exporter(protocol=protocol, api_key="api", secret_key="secret")
    assert exporter.kwargs["headers"] == headers


@pytest.mark.parametrize("key", sorted(RESERVED_RESOURCE_KEYS) + ["fi.anything", "fi."])
async def test_reserved_resource_keys(key, recording_exporter):
    assert is_reserved_resource_key(key)
    with pytest.raises(ValueError) as exc:
        await create_exporter(resource_attributes={key: MARKER})
    assert str(exc.value) == f"resource_attributes contains reserved key(s): {key}"
    assert MARKER not in str(exc.value)
    assert recording_exporter == []


def test_reserved_key_errors_are_sorted():
    with pytest.raises(ValueError) as exc:
        build_resource_attributes("project", "observe", {"project_type": MARKER, "fi.anything": MARKER})
    assert str(exc.value) == "resource_attributes contains reserved key(s): fi.anything, project_type"


async def test_resource_passthrough_and_service_override(recording_exporter):
    user = {"service.name": "custom-service", "deployment.environment": "test"}
    exporter = await create_exporter(resource_attributes=user)
    assert exporter.kwargs["resource_attributes"] == {"project_name": "project", "project_type": "observe", **user}
    assert user == {"service.name": "custom-service", "deployment.environment": "test"}
    assert not is_reserved_resource_key("service.name")


@pytest.mark.parametrize("values", [{
    "project_type": "experiment"
}, {
    "resource_attributes": {
        "a": 1
    }
}, {
    "protocol": "https"
}])
def test_invalid_config_types(values):
    with pytest.raises(ValidationError):
        otel_register.FutureAGITelemetryExporter(**values)


async def test_observe_routing_is_deterministic_without_version_or_eval_keys(recording_exporter):
    first = await create_exporter()
    second = await create_exporter()
    assert first.kwargs == second.kwargs
    assert first.kwargs["resource_attributes"] == {
        "project_name": "project", "project_type": "observe", "service.name": "project"
    }


async def test_batch_kwargs_forwarded_without_tls_overrides(recording_exporter):
    batching = {
        "batch_size": 7,
        "flush_interval": 0.25,
        "max_queue_size": 13,
        "drop_on_overflow": True,
        "shutdown_timeout": 3.0
    }
    exporter = await create_exporter(**batching)
    for key, value in batching.items():
        assert exporter.kwargs[key] == value
    assert set(exporter.kwargs) == {"endpoint", "headers", "protocol", "resource_attributes", *batching}
    assert "insecure" not in exporter.kwargs
    assert "certificate_file" not in exporter.kwargs


@pytest.mark.parametrize("protocol", ["http", "grpc"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_warnings_and_debug_logs_do_not_expose_secrets(protocol, scheme, recording_exporter, caplog):
    caplog.set_level(logging.DEBUG)
    endpoint = f"{scheme}://collector.example/{MARKER}"
    exporter = await create_exporter(protocol=protocol, endpoint=endpoint)
    assert exporter.kwargs["endpoint"] == endpoint
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    expected = []
    if scheme == "http":
        expected.append("futureagi exporter: plaintext endpoint configured; credentials and payload are unencrypted")
    if protocol == "grpc":
        expected.append("futureagi exporter: gRPC endpoint path is ignored by the OTLP gRPC exporter")
    assert warnings == expected
    assert MARKER not in caplog.text


def test_model_dump_contains_secrets_and_must_not_be_logged():
    """SerializableSecretStr intentionally serializes plaintext; factory code must never log config dumps (FR-15)."""
    config = otel_register.FutureAGITelemetryExporter(api_key=f"api-{MARKER}", secret_key=f"secret-{MARKER}")
    dumped = config.model_dump()
    assert dumped["api_key"] == f"api-{MARKER}"
    assert dumped["secret_key"] == f"secret-{MARKER}"
