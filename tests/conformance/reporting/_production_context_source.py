"""Independent provider authorization persisted across the service restart."""

import json
import sqlite3

from adcp.reporting.ledger.models import ReportingConfigurationGenerationKey
from adcp.reporting.production.contracts import ReportingProductionSourceBinding

from ._production_support import Source


class DurableBindingSource(Source):
    def __init__(self, key, path, rows=None, **kwargs):
        super().__init__(key, path, rows, **kwargs)
        self.binding_path = path.with_suffix(".bindings")
        with sqlite3.connect(self.binding_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bindings (account TEXT, config TEXT, version INTEGER,"
                " document TEXT, PRIMARY KEY(account,config,version))"
            )

    def bind_generation(self, configuration, **kwargs):
        binding = super().bind_generation(configuration, **kwargs)
        key = configuration.generation_key
        with sqlite3.connect(self.binding_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO bindings VALUES (?,?,?,?)",
                (
                    key.account_id,
                    key.delivery_config_id,
                    key.delivery_config_version,
                    json.dumps(binding.document()),
                ),
            )
        return binding

    def configuration_binding(self, configuration):
        key = configuration.generation_key
        with sqlite3.connect(self.binding_path) as db:
            row = db.execute(
                "SELECT document FROM bindings WHERE account=? AND config=? AND version=?",
                (key.account_id, key.delivery_config_id, key.delivery_config_version),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return ReportingProductionSourceBinding(
            ReportingConfigurationGenerationKey(
                value["account_id"], value["delivery_config_id"], value["delivery_config_version"]
            ),
            value["capabilities_sha256"],
            tuple(tuple(pair) for pair in value["media_buy_products"]),
            configuration_sha256=value["configuration_sha256"],
        )
