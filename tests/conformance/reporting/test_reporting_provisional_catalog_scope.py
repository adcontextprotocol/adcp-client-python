"""Fresh provisional readiness examines only its exact catalog identities."""

import json
from importlib.resources import files

import pytest

from adcp.reporting.ledger import LedgerConflictError
from adcp.reporting.outbox._schema import (
    PROVISIONAL_REQUIRED_OBJECTS,
    REQUIRED_OBJECTS,
    _catalog_objects,
    schema_objects,
    validate_provisional_schema,
)
from adcp.reporting.outbox.status_schema import REQUIRED_STATUS_OBJECTS
from adcp.reporting.receipts import PgReportingReceiptStore

from ._generation_support import isolated_reporting_pool

TABLES = ("reporting_provisional_acquisitions", "reporting_provisional_observations")
FUNCTION = ("reporting_provisional_immutable", "")


class CatalogProbe:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    async def execute(self, query, parameters=None):
        rows = await (await self.connection.execute(query, parameters)).fetchall()
        self.calls.append((query, parameters, rows))

        class Cursor:
            async def fetchall(self):
                return rows

        return Cursor()

    def assert_narrow_scope(self):
        assert len(self.calls) == 6
        assert self.calls[0][1] == (list(TABLES),)
        assert {row[1] for row in self.calls[0][2]} == set(TABLES)
        oids = [row[0] for row in self.calls[0][2]]
        for _, parameters, _ in self.calls[1:5]:
            assert parameters == (oids,)
        assert self.calls[5][1] == FUNCTION
        assert [(row[0], row[1]) for row in self.calls[5][2]] == [FUNCTION]
        assert [len(rows) for _, _, rows in self.calls] == [2, 10, 8, 4, 2, 1]


@pytest.mark.parametrize("autocommit", [False, True])
async def test_exact_catalog_scope_preserves_full_contract_and_excludes_overload(autocommit):
    expected = {
        **REQUIRED_OBJECTS,
        **{
            key: value
            for key, value in REQUIRED_STATUS_OBJECTS.items()
            if "reporting_issue_waiver_bindings" in key
        },
        **PROVISIONAL_REQUIRED_OBJECTS,
    }
    for package in ("materializer", "receipts"):
        expected.update(
            json.loads(
                files("adcp.reporting." + package).joinpath("required_schema.json").read_text()
            )
        )
    assert len(expected) == 790 and len(PROVISIONAL_REQUIRED_OBJECTS) == 27
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await PgReportingReceiptStore(pool=pool).create_schema()
        async with pool.connection() as connection:
            full = CatalogProbe(connection)
            assert await schema_objects(full) == expected
            assert len(full.calls) == 6
            assert sum(len(rows) for _, _, rows in full.calls) == 790
            await connection.execute(
                "CREATE TABLE reporting_unrelated_catalog_scope (value integer);"
                " CREATE FUNCTION reporting_provisional_immutable(integer) RETURNS integer"
                " LANGUAGE sql IMMUTABLE AS 'SELECT $1'"
            )
            default = CatalogProbe(connection)
            objects = await schema_objects(default)
            assert {key: objects[key] for key in expected} == expected
            assert set(objects) - set(expected) == {
                "table:reporting_unrelated_catalog_scope",
                "column:reporting_unrelated_catalog_scope.value",
                "function:reporting_provisional_immutable(integer)",
            }
            narrow = CatalogProbe(connection)
            assert (
                await _catalog_objects(narrow, table_names=TABLES, function_identity=FUNCTION)
                == PROVISIONAL_REQUIRED_OBJECTS
            )
            narrow.assert_narrow_scope()
            protected = CatalogProbe(connection)
            await validate_provisional_schema(protected)
            protected.assert_narrow_scope()
            print(
                json.dumps(
                    {
                        "catalog_scope": {
                            "autocommit": autocommit,
                            "full_queries": len(full.calls),
                            "full_rows": [len(rows) for _, _, rows in full.calls],
                            "narrow_queries": len(protected.calls),
                            "narrow_rows": [len(rows) for _, _, rows in protected.calls],
                            "default_extra_objects": len(objects) - len(expected),
                        }
                    }
                ),
                flush=True,
            )
            # A prior successful validation cannot mask later DDL. The overload
            # must neither satisfy nor perturb the exact zero-argument identity.
            await connection.execute("DROP FUNCTION reporting_provisional_immutable() CASCADE")
            with pytest.raises(
                LedgerConflictError,
                match=r"missing:function:reporting_provisional_immutable\(\)",
            ) as failure:
                await validate_provisional_schema(connection)
            assert failure.value.code == "PROVISIONAL_SCHEMA_UNREADY"
            assert (
                await (
                    await connection.execute("SELECT reporting_provisional_immutable(7)")
                ).fetchone()
            ) == (7,)
