"""Regression tests for idempotent OCPP 1.6 remote starts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ocpp.v16 import call
from ocpp.v16.enums import ChargePointStatus, RemoteStartStopStatus

from custom_components.ocpp.chargepoint import _ConnectorAwareMetrics
from custom_components.ocpp.ocppv16 import ChargePoint


@pytest.fixture
def cp():
    """Build a charge point with mocked transport and HA updates."""
    cp = object.__new__(ChargePoint)
    cp._active_tx = {}
    cp._applied_limits = {}
    cp.active_transaction_id = 0
    cp.num_connectors = 1
    cp._remote_id_tag = "remote-tag"
    cp.call = AsyncMock(
        return_value=SimpleNamespace(status=RemoteStartStopStatus.accepted)
    )
    cp.notify_ha = AsyncMock()
    cp._metrics = _ConnectorAwareMetrics()
    cp._charger_reports_session_energy = False
    cp.settings = SimpleNamespace(cpid="test-cp")
    cp.update = Mock(return_value=None)
    cp.hass = SimpleNamespace(async_create_task=Mock())
    return cp


@pytest.mark.parametrize("connector_id", [1, 2])
async def test_active_transaction_is_noop(cp, connector_id):
    """An active transaction suppresses the command on either connector."""
    cp.num_connectors = 2
    cp._active_tx = {connector_id: 123}

    assert await cp.start_transaction(connector_id) is True

    cp.call.assert_not_awaited()
    cp.notify_ha.assert_not_awaited()


@pytest.mark.parametrize("connector_id", [0, 1])
async def test_single_connector_legacy_transaction_is_noop(cp, connector_id):
    """The legacy record protects starts before the map is reconstructed."""
    cp.active_transaction_id = 123

    assert await cp.start_transaction(connector_id) is True

    cp.call.assert_not_awaited()
    cp.notify_ha.assert_not_awaited()


@pytest.mark.parametrize(
    ("active_tx", "legacy_tx", "num_connectors", "connector_id"),
    [
        ({}, 0, 1, 1),
        ({1: 0}, 0, 1, 1),
        ({1: 0}, 123, 1, 1),
        ({2: 123}, 123, 2, 1),
        ({}, 123, 2, 1),
        ({}, 123, 1, 2),
        ({}, 123, None, 1),
        ({}, 123, "unknown", 1),
    ],
)
async def test_normal_start(cp, active_tx, legacy_tx, num_connectors, connector_id):
    """Idle connectors send a start without borrowing another transaction."""
    cp._active_tx = active_tx
    cp.active_transaction_id = legacy_tx
    cp.num_connectors = num_connectors

    assert await cp.start_transaction(connector_id) is True

    cp.call.assert_awaited_once_with(
        call.RemoteStartTransaction(connector_id=connector_id, id_tag="remote-tag")
    )
    cp.notify_ha.assert_not_awaited()


async def test_rejected_start(cp):
    """A rejected normal start still returns false and notifies HA."""
    cp.call.return_value.status = RemoteStartStopStatus.rejected

    assert await cp.start_transaction(1) is False

    cp.call.assert_awaited_once()
    cp.notify_ha.assert_awaited_once()


async def test_suspended_ev_then_stop_allows_start(cp):
    """Suspension keeps the transaction; StopTransaction permits a new start."""
    cp._active_tx = {1: 123}
    cp.active_transaction_id = 123
    cp.on_status_notification(
        connector_id=1, error_code="NoError", status=ChargePointStatus.suspended_ev
    )
    assert cp._active_tx == {1: 123}
    assert cp.active_transaction_id == 123
    assert await cp.start_transaction(1) is True
    cp.call.assert_not_awaited()

    cp.on_stop_transaction(
        meter_stop=1000, timestamp="2026-09-27T00:00:00Z", transaction_id=123
    )
    assert cp._active_tx == {1: 0}
    assert cp.active_transaction_id == 0
    assert await cp.start_transaction(1) is True
    cp.call.assert_awaited_once_with(
        call.RemoteStartTransaction(connector_id=1, id_tag="remote-tag")
    )
