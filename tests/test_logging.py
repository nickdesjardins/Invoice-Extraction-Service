"""Tests for what a successful extraction leaves in the service's log.

On a public deployment the documents are visitors' documents, and a host keeps its
logs for days. So the line written for every extraction says which model answered
and how long it took, and nothing the document said. The vendor name and the
invoice number are still there for a developer who switches DEBUG on.
"""

from __future__ import annotations

import logging
from typing import Callable

import httpx
import pytest

from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    http,
    ok,
    router,
)
from tests.test_playground import result_of

#: What the sample invoice says. Neither may appear in a line written at INFO or above.
VENDOR = "Northwind Traders Inc."
NUMBER = "NW-2024-0187"

ROUTES = ["/api/v1/extract", "/playground/extract"]


async def _extract(http: httpx.AsyncClient, route: str) -> None:
    response = await http.post(route, json={"text": SAMPLE_INVOICE_TEXT})
    status = response.status_code if route == ROUTES[0] else result_of(response)["status_code"]
    assert status == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_an_extraction_is_logged_without_what_the_document_said(
    route: str,
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    router(default=ok())

    with caplog.at_level(logging.INFO):
        await _extract(http, route)

    lines = [record.getMessage() for record in caplog.records if record.levelno >= logging.INFO]
    written = [line for line in lines if "extracted invoice" in line]
    assert len(written) == 1, lines
    assert "model=" in written[0] and "elapsed_ms=" in written[0]
    for line in lines:
        assert VENDOR not in line and NUMBER not in line, line


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_a_developer_can_still_see_what_was_extracted(
    route: str,
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    router(default=ok())

    with caplog.at_level(logging.DEBUG, logger="app"):
        await _extract(http, route)

    told = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG and VENDOR in record.getMessage()
    ]
    assert len(told) == 1 and NUMBER in told[0], [record.getMessage() for record in caplog.records]
