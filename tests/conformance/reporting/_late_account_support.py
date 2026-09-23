"""Independent currency contracts and persistent adopter I/O for public progress tests."""

import base64
import hashlib
import json
from dataclasses import replace

import rfc8785

from adcp.reporting.inline_source import InlineFetchResult
from adcp.reporting.materializer import ReportingRevisionVerifier, reference_verifier
from adcp.reporting.materializer.contracts import failure

from ._production_support import Source, SQLiteDestination

ACCOUNTS = {"usd": ("USD", 503), "eur": ("EUR", 3)}


def rows_for(account):
    currency, count = ACCOUNTS[account]
    return [
        {
            "row_id": f"{number:06d}",
            "impressions": number % 3,
            "spend": "1.25",
            "currency": currency,
            "details": {"active": True, "values": [1, None, "é", "e\u0301"]},
        }
        for number in range(count)
    ]


class AccountSource(Source):
    async def fetch(self, request):
        account = str(request.identity.account_id)
        self.requests.append(request)
        return InlineFetchResult(
            rows_for(account), data_through=request.period.end, currency=ACCOUNTS[account][0]
        )


class CurrencyDestination(SQLiteDestination):
    """The same SQLite destination, with one independently pinned session per currency."""

    def __init__(self, path, keys):
        super().__init__(path, keys[0])
        self.readers = {key: SQLiteDestination(path, key) for key in keys}

    def resolve(self, request, *, phase, context):
        provider = self.readers.get(request.verification_key)
        if provider is None:
            raise failure("AUTHORIZATION_DENIED")
        return provider.resolve(request, phase=phase, context=context)


def verifier_for(currency, capability):
    base = reference_verifier(capability)
    if currency == "USD":
        return base
    assert currency == "EUR"
    definition = json.loads(base.definition_bytes)
    definition["report_definition_id"] = "reference-report-eur-v1"
    for metric in definition["metrics"]:
        if metric.get("unit") == "USD":
            metric["unit"] = "EUR"
    schema = json.loads(base.schema_bytes)
    schema["properties"]["currency"]["const"] = "EUR"
    schema["properties"]["spend"]["x-adcp-control-total"]["unit"] = "EUR"

    def encode(value):
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()

    schema_bytes = encode(schema)
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()
    contract = json.loads(base.canonicalization_bytes)
    contract["schema_sha256"] = schema_hash
    for vector in contract["golden_vectors"].values():
        for row in vector["input_rows"]:
            row["currency"] = "EUR"
        canonical = rfc8785.dumps(sorted(vector["input_rows"], key=lambda row: row["row_id"]))
        vector["canonical_utf8_base64"] = base64.b64encode(canonical).decode()
        vector["sha256"] = hashlib.sha256(canonical).hexdigest()
    definition_bytes, contract_bytes = encode(definition), encode(contract)
    key = replace(
        base.key,
        report_definition_id=definition["report_definition_id"],
        definition=replace(
            base.key.definition,
            report_definition_uri="https://contracts.example.test/reference-eur-definition.json",
            report_definition_sha256=hashlib.sha256(definition_bytes).hexdigest(),
            schema_uri="https://contracts.example.test/reference-eur-schema.json",
            schema_sha256=schema_hash,
            monetary_metric_units=(("spend", "EUR"),),
            monetary_control_total_units=(("spend", "EUR"),),
        ),
        canonicalization=replace(
            base.key.canonicalization,
            canonicalization_id="reference-eur-jcs-rows-v1",
            canonicalization_uri="https://contracts.example.test/reference-eur-canonicalization.json",
            canonicalization_sha256=hashlib.sha256(contract_bytes).hexdigest(),
        ),
    )
    return ReportingRevisionVerifier(key, definition_bytes, schema_bytes, contract_bytes)
