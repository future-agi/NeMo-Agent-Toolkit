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
"""Pure configuration helpers for Future AGI Observe trace export."""

import logging
import unicodedata
from collections.abc import Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FUTUREAGI_HTTP_BASE = "https://api.futureagi.com"
FUTUREAGI_HTTP_TRACES_PATH = "/tracer/v1/traces"
FUTUREAGI_GRPC_ENDPOINT = "https://grpc.futureagi.com"

# Same environment variable names, default (content exported) and replacement value as the Future AGI traceAI SDK.
FI_HIDE_INPUTS = "FI_HIDE_INPUTS"
FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS"
FUTUREAGI_REDACTED_VALUE = "__REDACTED__"
INPUT_CONTENT_ATTRIBUTES = ("input.value", "input.value_obj")
OUTPUT_CONTENT_ATTRIBUTES = ("output.value", "output.value_obj")

RESERVED_RESOURCE_KEYS = frozenset({
    "project_name",
    "project_type",
    "project_version_id",
    "project_version_name",
    "eval_tags",
    "metadata",
    "fi.project_id",
    "fi.org_id",
    "fi.workspace_id",
})


def resolve_project(config_value: str, env: Mapping[str, str]) -> str:
    """Resolve the required project name, preferring nonblank configuration."""
    project = config_value.strip() or env.get("FI_PROJECT_NAME", "").strip()
    if not project:
        raise ValueError("project is required for futureagi (set `project` on the exporter or FI_PROJECT_NAME)")
    return project


def resolve_credential(config_value: str | None, env_name: str, field_name: str, env: Mapping[str, str]) -> str:
    """Resolve a required credential without including its value in errors."""
    credential = (config_value or "").strip() or env.get(env_name, "").strip()
    if not credential:
        raise ValueError(f"{field_name} is required for futureagi (set `{field_name}` on the exporter or {env_name})")
    return credential


def validate_endpoint(url: str, protocol: str) -> str:
    """Validate an OTLP URL without exposing it in errors, preserving it verbatim."""
    if url != url.strip() or any(char.isspace() or unicodedata.category(char) == "Cc" for char in url):
        raise ValueError("endpoint must not contain whitespace or control characters")
    try:
        split = urlsplit(url)
        hostname = split.hostname
    except ValueError:
        raise ValueError("endpoint must be a valid URL") from None
    if split.scheme not in {"https", "http"}:
        raise ValueError("endpoint scheme must be https or http")
    if not hostname:
        raise ValueError("endpoint must contain a hostname")
    try:
        split.port
    except ValueError:
        raise ValueError("endpoint must contain a valid port") from None
    if split.username is not None or split.password is not None:
        raise ValueError("endpoint must not contain credentials")
    if "?" in url or "#" in url:
        raise ValueError("endpoint must not contain a query or fragment")
    return url


def derive_http_endpoint(base_url: str) -> str:
    """Replace a validated base URL's path with the Future AGI HTTP traces path."""
    validate_endpoint(base_url, "http")
    return urlsplit(base_url)._replace(path=FUTUREAGI_HTTP_TRACES_PATH, query="", fragment="").geturl()


def resolve_endpoint(config_endpoint: str | None, protocol: str, env: Mapping[str, str]) -> str:
    """Resolve an explicit endpoint, transport-specific environment URL, or hosted default."""
    if config_endpoint is not None:
        return validate_endpoint(config_endpoint, protocol)
    if protocol == "http":
        return derive_http_endpoint(env.get("FI_BASE_URL") or FUTUREAGI_HTTP_BASE)
    return validate_endpoint(env.get("FI_GRPC_URL") or FUTUREAGI_GRPC_ENDPOINT, protocol)


def build_auth_headers(api_key: str, secret_key: str, protocol: str) -> dict[str, str]:
    """Use lowercase metadata keys for gRPC, which rejects uppercase keys."""
    if protocol == "http":
        return {"X-Api-Key": api_key, "X-Secret-Key": secret_key}
    return {"x-api-key": api_key, "x-secret-key": secret_key}


def is_reserved_resource_key(key: str) -> bool:
    """Identify Future AGI routing and evaluation resource keys."""
    return key in RESERVED_RESOURCE_KEYS or key.startswith("fi.")


def resolve_hide_flag(config_value: bool | None, env_name: str, env: Mapping[str, str]) -> bool:
    """Resolve a content-hiding flag: explicit configuration, then `true`/`false` in the environment, else False."""
    if config_value is not None:
        return config_value
    raw = env.get(env_name)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in {"true", "false"}:
        return value == "true"
    logger.warning("futureagi exporter: %s must be true or false; span content is exported", env_name)
    return False


def hidden_content_attributes(hide_inputs: bool, hide_outputs: bool, span_prefix: str) -> list[str]:
    """List the span attributes to redact. The toolkit metadata attribute holds both inputs and outputs."""
    attributes = [
        *(INPUT_CONTENT_ATTRIBUTES if hide_inputs else ()), *(OUTPUT_CONTENT_ATTRIBUTES if hide_outputs else ())
    ]
    if attributes:
        attributes.append(f"{span_prefix}.metadata")
    return attributes


def build_resource_attributes(project: str, project_type: str, user_attributes: dict[str, str]) -> dict[str, str]:
    """Attach Observe routing attributes while allowing a custom service name."""
    bad = [key for key in user_attributes if is_reserved_resource_key(key)]
    if bad:
        raise ValueError("resource_attributes contains reserved key(s): " + ", ".join(sorted(bad)))
    return {"project_name": project, "project_type": project_type, "service.name": project, **user_attributes}
