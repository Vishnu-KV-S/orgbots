"""OpenTelemetry export — an organization's events to its own collector, as OTLP logs.

OTLP/HTTP with the JSON encoding (`POST {endpoint}/v1/logs`), which every OpenTelemetry
Collector, and most vendors' intake, accepts without a protobuf toolchain here. One
request per batch; a non-2xx answer is an `OTelError` with the collector's reason, and
the exporter keeps the batch for its next try.

Records are built by `runtime.runtime.telemetry` from the audit trail and the gateway's
decision log; this module only knows the wire format and the HTTP.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

SERVICE = "agent-org-runtime"
TIMEOUT_S = 15.0


class OTelError(Exception):
    """The collector could not be reached, or refused the batch. Shown to admins."""


def _value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in values.items() if v not in (None, "")]


def log_record(
    when: dt.datetime, name: str, attrs: dict[str, Any], *, severity: str = "INFO"
) -> dict[str, Any]:
    nanos = str(int(when.timestamp() * 1_000_000_000))
    number = {"INFO": 9, "WARN": 13, "ERROR": 17}.get(severity, 9)
    return {
        "timeUnixNano": nanos,
        "observedTimeUnixNano": nanos,
        "severityNumber": number,
        "severityText": severity,
        "body": {"stringValue": name},
        "attributes": attributes({"event.name": name, **attrs}),
    }


def payload(organization_id: str, scope: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": attributes(
                        {"service.name": SERVICE, "agent_org.organization_id": organization_id}
                    )
                },
                "scopeLogs": [{"scope": {"name": scope}, "logRecords": records}],
            }
        ]
    }


class OTLPSender:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def send(self, endpoint: str, headers: dict[str, str], body: dict[str, Any]) -> None:
        url = endpoint.rstrip("/")
        if not url.endswith("/v1/logs"):
            url += "/v1/logs"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as http:
                got = await http.post(
                    url, json=body, headers={**headers, "content-type": "application/json"}
                )
        except httpx.HTTPError as exc:
            raise OTelError(
                f"the collector at {url} is not reachable ({type(exc).__name__})"
            ) from exc
        if got.status_code >= 300:
            raise OTelError(f"the collector answered {got.status_code}: {got.text[:200]}")
