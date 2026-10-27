"""Seller upgrades must not silently change an existing account's address."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from adcp.account_identity import account_key_payload
from adcp.accounts import AccountRecord, AccountRegistry, InMemoryAccountStorage
from adcp.types.core import TaskResult, TaskStatus

KEY = {
    "brand": {"domain": "brand.example", "countries": ["US"]},
    "operator": "agency.example",
    "operator_unit": {"id": "seat-one"},
    "currency": "USD",
    "timezone": "UTC",
}
REDUCED = {
    "brand": {"domain": "brand.example"},
    "operator": "agency.example",
    "sandbox": False,
}


def registry(storage=None):
    client = SimpleNamespace(
        agent_config=SimpleNamespace(agent_uri="https://seller.example/mcp"),
        capabilities=None,
    )
    return AccountRegistry(client, storage)


async def provision(accounts, *, key=KEY, version="3.1", row_key=REDUCED):
    result = TaskResult(
        status=TaskStatus.COMPLETED,
        success=True,
        data={
            "accounts": [
                {
                    **deepcopy(row_key),
                    "account_id": "seller-account",
                    "status": "active",
                    "action": "created",
                }
            ]
        },
        metadata={"adcp_version_conversion": {"wire_version": version, "losses": []}},
    )
    await accounts.observe(
        "sync_accounts",
        {"accounts": [{**key, "billing": "operator"}]},
        result,
        fetch_capabilities=False,
    )


@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_provisioned_reduced_key_survives_seller_upgrade_and_restart(version):
    storage = InMemoryAccountStorage()
    accounts = registry(storage)
    await provision(accounts, version=version)
    record = await accounts.get(KEY)
    assert record.wire_key == REDUCED
    assert record.wire_version == version
    # A durable adapter retains the wire address under the complete buyer key.
    restarted = registry(storage)
    original = {"account": deepcopy(KEY), "brief": "video"}
    wire, pending = await restarted.preserve_wire_keys("get_products", original, version="3.2")
    assert original["account"] == KEY
    assert wire["account"] == REDUCED
    assert wire["account"].get("account_id") is None
    assert pending == [
        {"pointer": "/account", "provisioned_wire_version": version, "wire_version": "3.2"}
    ]


async def test_reduced_identity_survives_resync_without_losing_mutable_options():
    accounts = registry()
    await provision(accounts)
    entry = {**deepcopy(KEY), "billing": "brand", "payment_terms": "net_30"}
    entry["brand"]["name"] = "New display name"
    original = {"accounts": [entry]}
    wire, pending = await accounts.preserve_wire_keys("sync_accounts", original, version="3.2")
    assert account_key_payload(wire["accounts"][0]) == REDUCED
    assert wire["accounts"][0]["brand"]["name"] == "New display name"
    assert wire["accounts"][0]["billing"] == "brand"
    assert wire["accounts"][0]["payment_terms"] == "net_30"
    assert original["accounts"][0]["currency"] == "USD"
    assert pending[0]["pointer"] == "/accounts/0"
    await provision(accounts, version="3.2")
    assert (await accounts.get(KEY)).wire_version == "3.1"
    assert (await accounts.get(KEY)).wire_key == REDUCED


async def test_reduce_and_share_retains_each_full_key_without_new_refusal():
    accounts = registry()
    second = {**KEY, "currency": "EUR", "operator_unit": {"id": "seat-two"}}
    await provision(accounts)
    await provision(accounts, key=second)
    assert (await accounts.get(KEY)).wire_key == REDUCED
    assert (await accounts.get(second)).wire_key == REDUCED
    for key in (KEY, second):
        wire, pending = await accounts.preserve_wire_keys(
            "get_products", {"account": key}, version="3.2"
        )
        assert wire["account"] == REDUCED
        assert len(pending) == 1


async def test_old_contract_keeps_address_without_premature_migration_diagnostic():
    accounts = registry()
    await provision(accounts)
    wire, pending = await accounts.preserve_wire_keys(
        "get_products", {"account": KEY}, version="3.1"
    )
    assert wire["account"] == REDUCED
    assert pending == []


async def test_latest_provisioning_and_explicit_ids_need_no_identity_pin():
    accounts = registry()
    await provision(accounts, version="3.2", row_key=KEY)
    assert await accounts.get(KEY) == AccountRecord("seller-account", "active")
    for ref in (KEY, {"account_id": "external"}):
        wire, pending = await accounts.preserve_wire_keys(
            "get_products", {"account": ref}, version="3.2"
        )
        assert wire["account"] == ref
        assert pending == []


async def test_forgetting_account_removes_the_recorded_wire_address():
    accounts = registry()
    await provision(accounts)
    await accounts.forget(KEY)
    wire, pending = await accounts.preserve_wire_keys(
        "get_products", {"account": KEY}, version="3.2"
    )
    assert wire["account"] == KEY
    assert pending == []
