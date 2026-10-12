<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# NVIDIA NeMo Agent Toolkit: Observing a Workflow with Future AGI

This guide shows how to send OpenTelemetry traces from NeMo Agent Toolkit to Future AGI Observe using the built-in `futureagi` exporter in `nvidia-nat[opentelemetry]`. It exports traces only; evaluation experiments, version comparisons, logs, and metrics are not supported.

## Step 1: Prerequisites

You need a Future AGI Observe project, an API key, and a secret key authorized to send traces to that project. Install the OpenTelemetry extra and the calculator example from the repository root:

```bash
uv pip install -e ".[opentelemetry]"
uv pip install -e examples/observability/simple_calculator_observability/
```

LLM and tool spans are recorded by the profiler plugin. The development install (`uv sync --all-groups --extra most`) includes it; with a smaller install, also run `uv pip install -e ".[profiler]"`. Without it, traces contain only workflow and function spans.

Complete the [Simple Calculator prerequisites](../../../../examples/getting_started/simple_calculator/README.md), including the LLM credentials, before running the workflow.

## Step 2: Configure the environment

Supply credentials through environment variables, keeping their values out of YAML and source control:

```bash
export FI_API_KEY="<your-api-key>"
export FI_SECRET_KEY="<your-secret-key>"
export FI_PROJECT_NAME="simple_calculator"
```

The exporter reads these variables when the corresponding `api_key`, `secret_key`, or `project` field is empty or contains only whitespace. Nonblank configuration values take precedence, and surrounding whitespace is removed. Missing credentials or a missing project cause a configuration error before the exporter is created.

## Step 3: Configure tracing

Add the following section to a workflow configuration:

```yaml
general:
  telemetry:
    tracing:
      futureagi:
        _type: futureagi
        project: ${FI_PROJECT_NAME:-simple_calculator}
        # FI_API_KEY and FI_SECRET_KEY are read from the environment.
        # protocol: grpc  # HTTP is the default.
        project_type: observe
        resource_attributes:
          deployment.environment: development
        batch_size: 100
        flush_interval: 5.0
        max_queue_size: 1000
        drop_on_overflow: false
        shutdown_timeout: 10.0
```

Every trace resource includes `project_name`, `project_type: observe`, and `service.name` (the project name by default). Extra `resource_attributes` must have string values. You can override `service.name`, but routing and evaluation keys are reserved: `project_name`, `project_type`, `project_version_id`, `project_version_name`, `eval_tags`, `metadata`, and all keys beginning with `fi.`. No version or evaluation attributes are generated.

## Step 4: Run the calculator example

From the repository root:

```bash
nat run --config_file examples/observability/simple_calculator_observability/configs/config-futureagi.yml --input "What is 2 * 4?"
```

Open the Future AGI Observe project matching `FI_PROJECT_NAME` to view traces. The example defaults to the `simple_calculator` project if that variable is unset.

## HTTP, gRPC, and custom endpoints

| Transport | Configuration | Hosted endpoint | Optional environment override |
| --- | --- | --- | --- |
| HTTP | `protocol: http` (default) | `https://api.futureagi.com/tracer/v1/traces` | `FI_BASE_URL` |
| gRPC | `protocol: grpc` | `https://grpc.futureagi.com` | `FI_GRPC_URL` |

For HTTP, `FI_BASE_URL` supplies the scheme and host, including any port. Its path is replaced with the Future AGI trace ingestion path. For example, `https://collector.example:8443/old/` becomes `https://collector.example:8443/tracer/v1/traces`. For gRPC, `FI_GRPC_URL` supplies the collector URL.

For a self-hosted collector, set `endpoint` under the exporter to the complete OTLP URL. This takes precedence over both environment overrides and is used verbatim:

```yaml
_type: futureagi
endpoint: https://collector.example:8443/tracer/v1/traces
```

With `protocol: grpc`, use a collector URL such as `https://collector.example:4317`. A gRPC endpoint path is accepted with a warning because the underlying exporter ignores it. HTTP sends `X-Api-Key` and `X-Secret-Key` headers; gRPC sends lowercase `x-api-key` and `x-secret-key` metadata.

Endpoints must have an HTTP or HTTPS scheme, a hostname, and a valid port if specified. Embedded credentials, query strings, fragments, whitespace, and control characters are rejected, including in environment overrides.

## Security and private certificate authorities

Hosted defaults use HTTPS. Plaintext transport requires an explicit `http://` URL in `endpoint` or the relevant environment override. It emits a warning because credentials and trace payloads are unencrypted. Use HTTPS for remote collectors.

For a private certificate authority, set `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE` to the CA certificate file, or use the general `OTEL_EXPORTER_OTLP_CERTIFICATE` variable. Both transports use the standard OpenTelemetry certificate settings. The exporter does not disable TLS verification or provide an `insecure` configuration option.

Keep credential values out of YAML and logs. The toolkit's `SerializableSecretStr` serializes plaintext in `model_dump()`, so configuration dumps can expose credentials even when their normal representation is masked. The Future AGI factory does not log configuration objects, headers, endpoint values, or secrets.

## Troubleshooting

- **Missing project or credentials:** Set `FI_PROJECT_NAME`, `FI_API_KEY`, and `FI_SECRET_KEY`, or check that the corresponding fields are nonblank. Error messages identify the field and environment variable without printing credential values.
- **Authentication failure:** An HTTP `401` response or gRPC `UNAUTHENTICATED` status produces an `ERROR` log from the OpenTelemetry OTLP exporter. Verify both credentials and access to the Observe project. The workflow result is unaffected, but the rejected batch is not delivered; the exporter can still send subsequent batches.
- **Collector unreachable:** Verify the scheme, host, port, and HTTP ingestion path. Export failures are logged and do not change the workflow result. Set `OTEL_EXPORTER_OTLP_TIMEOUT` (or the trace-specific `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`) to bound export attempts.
- **Missing final spans:** Pending spans are flushed on orderly shutdown within `shutdown_timeout`. Keep this timeout long enough for the collector to respond. A full queue drops incoming spans when `drop_on_overflow: true`; otherwise it forces a flush to make space.
