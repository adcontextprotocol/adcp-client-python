"""Read-only verification of the installed transactional reporting chain.

The contract covers column types/nullability/defaults, validated constraints,
usable indexes, enabled triggers, and the actual guard function definitions.
Presence of tables, a version marker, or a caller-supplied capability is not a
readiness check. Catalog reads are schema-scoped; they read no tenant records.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError

# Generated from the bundled five-step chain. Deliberate schema changes must
# update this contract and exercise both fresh and populated upgrade paths.
SCHEMA_CONTRACT: dict[str, str] = {
    "function:reporting_adjustment_evidence_immutable()": (
        "6435361e1fcbbf1926ee1e11f13897fb" "9a660ccc5e5f4bbe93114077c1bea636"
    ),
    "function:reporting_canonical_evidence_immutable()": (
        "7c62d73989b1ddcff1340e4f2108bcb8" "60bd09c54659094260aff1ee3c8af335"
    ),
    "function:reporting_canonical_json(document jsonb)": (
        "d50759bae48ef38808360d4a59f55978" "0eb1d1b934c2ded883cb7e918159c3e9"
    ),
    "function:reporting_identity_sha256(value text)": (
        "93224ff42ac26d1550e63a3726227f7e" "2ba55fea2a3b5af8f6f6879f649432f5"
    ),
    "function:reporting_iso_utc(moment timestamp with time zone)": (
        "af73b04da507f3d5d8fc0994aa693a7e" "621fb7e339f380e309beb5af10c60058"
    ),
    "function:reporting_notification_immutable()": (
        "8bfe540ace33195ad5588d04e869a870" "3fa162501aef6e955b897f1cc9413d14"
    ),
    "function:reporting_obligation_currency_immutable()": (
        "03e654a4967f2e8d174c1530d49de312" "540891baf061a341669bfc15112496e9"
    ),
    "function:reporting_payload_keys(document jsonb)": (
        "48b1d45bddd9a43affd0b63ad0eb8874" "73849985d82ded01a6a5cae6d108b476"
    ),
    "function:reporting_payload_sha256(document jsonb)": (
        "b08900f152a3c21c0ba84e5f89826b9f" "29967837d5277ade6ccc8de0e84eaa0d"
    ),
    "function:reporting_receipt_advance()": (
        "9869d9d96df91934b82f83a7c6f1f1ca" "3e6139358424646700e1f42d2c1ab6d7"
    ),
    "function:reporting_receipt_head_exact()": (
        "7f8887f5ea1a5a938fb8dd789398adc8" "8461927cda4b80b3b7196d5d665d88cd"
    ),
    "function:reporting_receipt_terminal()": (
        "bff86010f9ac57af1eee22a78a7ab461" "6ed34185a06431f3bf0b138ea2bc4386"
    ),
    "function:reporting_reconciliation_change_guard()": (
        "5844dc57d056f89d74ecd938f41674cd" "1994cbf90132ba567f5953d3a9aaacef"
    ),
    "function:reporting_reconciliation_evidence(r reporting_reconciliation_records)": (
        "60e89af5cb7a88aae39da7875aa0bb7e" "daa361cc3fbd5d2ebb9c0e0009aa48ea"
    ),
    "function:reporting_reconciliation_guard()": (
        "a7624ca621e3c95932cd23f5b6683af1" "16463984439e567ed3fbbe64969b7da3"
    ),
    "function:reporting_reconciliation_head_guard()": (
        "a92fd0d4b284d1a75aab0d2afb9e3ea7" "0829118d3d970427482ac5f5a75ce7c1"
    ),
    "function:reporting_reconciliation_immutable()": (
        "cb1b0e47e2848ae79292f539f4c3fe85" "8b651db28434bdd468596b36cd08d9cb"
    ),
    "function:reporting_reconciliation_reference_immutable()": (
        "6461682d3ebaceb7ccd795f1d87c524a" "02f8d1f1487dd182e23779b44dae4ddf"
    ),
    "function:reporting_reconciliation_validate(r reporting_reconciliation_records)": (
        "309164066f02cc4d0f3ff66df47cf5c4" "ddddcc69525436634f94845e0d2864ed"
    ),
    "function:reporting_sorted_strings(document jsonb)": (
        "9c5ec19205737071bd5f9fe79563397b" "63ee5105cb190a4d2db01dc414691736"
    ),
    "function:reporting_sorted_totals(document jsonb)": (
        "d29826abcc96b3de6a27851d088376e8" "f9925d59d785c05fecec0bf6dbdc721c"
    ),
    "function:reporting_wire_digest(document jsonb)": (
        "eba0b3444ee72fbf3d5c467c6ca2c68c" "2dd057b82e4dfd24ae10688a1e0e53af"
    ),
    "reporting_adjustments:columns": (
        "21f185134755dc64bf18f6650712ae1d" "06405e92e26a75913ed26ee239cfa37a"
    ),
    "reporting_adjustments:constraints": (
        "603d958337b456b6130c75cee2c6b9e5" "114a40fa53f1f59be86c421215a0a5af"
    ),
    "reporting_adjustments:indexes": (
        "bb16320d04f5cc3c1d36ee1e1a1d63ab" "e4a3901f9e9ad0505104f4b7dd8983b4"
    ),
    "reporting_adjustments:triggers": (
        "307588c8df362a216195f22724c5b29e" "9751c687cc36c9990bba82979a1889e7"
    ),
    "reporting_configurations:columns": (
        "e8f6dd2f4d6b8cc90e578dbaef272c46" "57c231db0a6a1989db3a5f2ab89a7326"
    ),
    "reporting_configurations:constraints": (
        "8fe2605ba4fdf546e2ae09dad3c8f506" "96be067933dfa8ef1601633693edd6a9"
    ),
    "reporting_configurations:indexes": (
        "0691ad10621df08ea0876d1a705c2d43" "7dc06a21350f64b481056f1e70d3d13f"
    ),
    "reporting_configurations:triggers": (
        "a089938618c994c2c0a6d12993bb8078" "07d3f8eda1874d540e2f8f95fcb04a5d"
    ),
    "reporting_consumer_statuses:columns": (
        "ec88098deb17a31cb3d84c7b4d50c07a" "c81f32f3780f20532ca9bef2845ce2e0"
    ),
    "reporting_consumer_statuses:constraints": (
        "45afdf7bcf4cce7974ab6b0714c036fb" "c9d1a01d0bcb33052fcf74d8d098b5eb"
    ),
    "reporting_consumer_statuses:indexes": (
        "d9b2b4ef81d2809cafa3da5aec6a92dc" "8e0c6fb11ada4ab59fefe9a363dbdb0b"
    ),
    "reporting_consumer_statuses:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_issue_lifecycle:columns": (
        "1291ebaa7ee1fd72bfabe1a72d6a10fc" "3bc0a7429ce2efe48e3974901c550d1b"
    ),
    "reporting_issue_lifecycle:constraints": (
        "60f4b8b3c39bc0f413f32541c2eb3ebc" "2bd0978d97ad2dab22fa57582b537c74"
    ),
    "reporting_issue_lifecycle:indexes": (
        "f8869b28a542094006b97cc10ac4927c" "888e5744ba5035fa9c124f94b0cad9eb"
    ),
    "reporting_issue_lifecycle:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_issue_status_scopes:columns": (
        "8af4b6a4cf02f0beb52988a124cd0d44" "dd00170bfb27ddf8840aa181f5e35379"
    ),
    "reporting_issue_status_scopes:constraints": (
        "7be693d1c5e228f0b61bd2aca8470328" "7478623259546a0dda0ccc2df2071590"
    ),
    "reporting_issue_status_scopes:indexes": (
        "239b1b0172b2335f81b32d5b1cf72fc3" "f04cf4c30a0ec40b66ae87e9f35343ed"
    ),
    "reporting_issue_status_scopes:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_ledger_changes:columns": (
        "d6f6a70deebd30f0a0f3cfac3d6e9581" "1b2d60a4204d2949b2abe898e432cd1f"
    ),
    "reporting_ledger_changes:constraints": (
        "0f56059184f30d8e32e6323015b0ca5b" "19cb7f4865c73ca3bb2bd4459005de91"
    ),
    "reporting_ledger_changes:indexes": (
        "04708d175375319a7fdad3385b5fd944" "30b64cd55ef9d79b7d01dec8401faa88"
    ),
    "reporting_ledger_changes:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_notification_deliveries:columns": (
        "d96c42f82f3092c8cdee3bfd5712cc49" "34efffcf0d8d4e0c5a32ef8782cc3fa8"
    ),
    "reporting_notification_deliveries:constraints": (
        "9934d10675a54cc3cb4bf9dfde0b083c" "9bdc2df2c71f6d94d75a8629a8ef1240"
    ),
    "reporting_notification_deliveries:indexes": (
        "b393111322b0cb8ab8ea5a75078411e5" "4ce87a8770ecb5509ddcc92c36289fe9"
    ),
    "reporting_notification_deliveries:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_notification_events:columns": (
        "7ca4437ca676ca4abeb9ee97e17277f1" "0d7ec0f5cdab2d4d64bd7ea75042f2b7"
    ),
    "reporting_notification_events:constraints": (
        "043076e5170a06867999db8a1c8d1589" "56be50116ec96647be88ea1a613d03b8"
    ),
    "reporting_notification_events:indexes": (
        "0f014658961d9cc532c4293b63ca62f7" "2fea2d377d0614568f5eab358bfea8fa"
    ),
    "reporting_notification_events:triggers": (
        "2e8fabd912e2ab93f4c2f888b7a75000" "1ef85e3c5779f0cb21ce4b2a0fc583fd"
    ),
    "reporting_notification_expansions:columns": (
        "716e73267e27ea1122e48821d996df76" "739f47e7560bb23648dab5efb2483577"
    ),
    "reporting_notification_expansions:constraints": (
        "d3256a3b17badf25b73f9ff62cf7c883" "fbdaa8915f11cf5e2d6e3a875bcda7c0"
    ),
    "reporting_notification_expansions:indexes": (
        "d8312996f9b5dc4c060ce308bb076a8a" "8aee45dc95d55f38f7475dc677868060"
    ),
    "reporting_notification_expansions:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_obligations:columns": (
        "ed51fd558b29f46b11fa84abb0125af1" "be0d8ec3020df0bee4955b85f354fc75"
    ),
    "reporting_obligations:constraints": (
        "38594ba5b18657441707ac036d1b3a00" "93a41d4759acf98269e9bab44bda2940"
    ),
    "reporting_obligations:indexes": (
        "1c4f8f60d5a93eae9a7017640d958737" "220c7d57a3e9ef7ce1ce8fcd68b22869"
    ),
    "reporting_obligations:triggers": (
        "497b6f5ef2514960254fc0975bfe5cb6" "36e529634eb9baae027d14ec4cb0353e"
    ),
    "reporting_receipt_heads:columns": (
        "df430b84134c9d75645eacf3c78d5794" "ffabfae82a291970e35f16c5ce029d53"
    ),
    "reporting_receipt_heads:constraints": (
        "c6913cfe5697563e9477ce428ce672fc" "ef50c9255ac950b311c6cb4220349876"
    ),
    "reporting_receipt_heads:indexes": (
        "7e2484d7965549de6455c430af370cca" "1d9d645f3ab725c9da140aeab8d43d1b"
    ),
    "reporting_receipt_heads:triggers": (
        "f15506c66bc42c0df42a30e82a3d0bdc" "d739d50e7322c60574cb6bf8bf46ab25"
    ),
    "reporting_reconciliation_changes:columns": (
        "1b74719a83f904f551236a99149e4cc0" "3801b13a21bc0d20eee277c9a159b642"
    ),
    "reporting_reconciliation_changes:constraints": (
        "796834b9cebe998250662fcfb69a8d1c" "a66e759231ba4589fe35a813c7978044"
    ),
    "reporting_reconciliation_changes:indexes": (
        "c16f715b3cafbd731fd2564380c2bce7" "a8800fc72fdf8792bd8f569b36d93125"
    ),
    "reporting_reconciliation_changes:triggers": (
        "9cfc23afa441c2cd92fb7f653d86fe42" "e6b421a125cc654fbf910ef44e860622"
    ),
    "reporting_reconciliation_heads:columns": (
        "2fee5528f8f85bd6d40cc4b458a78c46" "de462d460a115c7ee9856f03846e7d2f"
    ),
    "reporting_reconciliation_heads:constraints": (
        "e0b0d4037ec594e3ca0c533d79504ac6" "71a29a6b5e334b8df5511beaea0a8ada"
    ),
    "reporting_reconciliation_heads:indexes": (
        "231d8a63c10130aaf95cbd801c68335e" "060dbd38d6c4e0c465cdb7170c56f635"
    ),
    "reporting_reconciliation_heads:triggers": (
        "571348e9aa132f91ba2452b4d1cf8205" "952f3075ce8e6320e976445a471385e8"
    ),
    "reporting_reconciliation_records:columns": (
        "a7ec44cc41ad8cf17c9d1ffe8e9552f9" "dcda5fb434c95362d224a0e335db60aa"
    ),
    "reporting_reconciliation_records:constraints": (
        "184cda4ff4e6fd0d340c3238ad549068" "d7e07fe5628f030be0f9113a7bf694c7"
    ),
    "reporting_reconciliation_records:indexes": (
        "6beb345279c860a3d67bf55d00d9f723" "ab7bdd685b04c5d1f8e463f6ad29df98"
    ),
    "reporting_reconciliation_records:triggers": (
        "7fc917df860adedda86f1cbd68cccbf9" "f51b5d34d0eacb35c8bd645c8e230007"
    ),
    "reporting_revision_rows:columns": (
        "133e7beac71a52ea7c25a71b4f296297" "ecde85780eb6a28f4ff6d9847785861b"
    ),
    "reporting_revision_rows:constraints": (
        "829c1080b2db3344e8a67d30934a56b7" "6f934908148d662ea299c36cac1b40e2"
    ),
    "reporting_revision_rows:indexes": (
        "6aebb2613c50d6153b0edfe91db2a5b0" "7d5a3f8ddd90e3398c15b952c87de56b"
    ),
    "reporting_revision_rows:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_revisions:columns": (
        "d861397cc8fd6986a387740a8f4a6084" "69504715e967ec438ff7d6af1aaa9540"
    ),
    "reporting_revisions:constraints": (
        "b6ed0e9e662bdfa3c2369fe9537b64a2" "9126e62a0f34c2c332824c51447078cb"
    ),
    "reporting_revisions:indexes": (
        "26df1e37323d185f21ef652224c15445" "39d03eeef282137dc8fdf86406fc4af0"
    ),
    "reporting_revisions:triggers": (
        "ab2267a69931c6ea860822156376d227" "28fbc7304b4ac7f9193afb3141448e16"
    ),
    "reporting_status_checkpoints:columns": (
        "665a64ed1e0f1546c4e15dc60cc71ec3" "16ecb701ca87ea809263c3ad1499388b"
    ),
    "reporting_status_checkpoints:constraints": (
        "936a423446f30aa77a1a30e2629f5da8" "4b798934f24ef855d0ec40bc50089dff"
    ),
    "reporting_status_checkpoints:indexes": (
        "9827d3c92209487636d64391d156414b" "9a36cef0d9971a47613b3df1bf572d01"
    ),
    "reporting_status_checkpoints:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
    "reporting_status_dirty:columns": (
        "d93019d63daca890b024760abaf313d5" "3cce8be654f6951ac7c1d5c98be47f34"
    ),
    "reporting_status_dirty:constraints": (
        "a9623edb2feb4e3be913b9ee8eac1ae0" "5d301d0fedf997fc77cfb12adc76fe76"
    ),
    "reporting_status_dirty:indexes": (
        "b4542cf6d4be2a90969056d33ca6a61f" "9fdf2cd02be2f59a44913ec3e835bae2"
    ),
    "reporting_status_dirty:triggers": (
        "f8812273d8b9c664f5154737298c5837" "1e57e910b8a72a5b3b1ea73427414db9"
    ),
    "reporting_status_dirty_heads:columns": (
        "29981f0f8145ce9a2f07370fe19550ce" "076fcd8faf32d627453566f9998aa11b"
    ),
    "reporting_status_dirty_heads:constraints": (
        "a21b12facd96ab7c2031cf0b7e1a57ec" "112f60f369fa393680e87b6b6de9fa79"
    ),
    "reporting_status_dirty_heads:indexes": (
        "90afb31ce0eccf93a47bdcef2334dafb" "fe09a73fba9c55db977d67871a7221ae"
    ),
    "reporting_status_dirty_heads:triggers": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5" "ed12ab4d8e11ba873c2f11161202b945"
    ),
}


async def schema_contract(connection: Any) -> dict[str, str]:
    tables = await (
        await connection.execute(
            "SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = current_schema() AND c.relkind = 'r'"
            " AND starts_with(c.relname, 'reporting_') ORDER BY c.relname"
        )
    ).fetchall()
    result: dict[str, str] = {}
    for oid, name in tables:
        columns = await (
            await connection.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, co.collname,"
                " pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a"
                " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                " LEFT JOIN pg_collation co ON co.oid = a.attcollation"
                " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attname",
                (oid,),
            )
        ).fetchall()
        constraints = await (
            await connection.execute(
                "SELECT contype, pg_get_constraintdef(oid), convalidated FROM pg_constraint"
                " WHERE conrelid = %s ORDER BY contype, pg_get_constraintdef(oid)",
                (oid,),
            )
        ).fetchall()
        indexes = await (
            await connection.execute(
                "SELECT i.indisunique, i.indisvalid, i.indisready,"
                " ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)"
                " FROM generate_series(1, i.indnatts) k), pg_get_expr(i.indpred, i.indrelid)"
                " FROM pg_index i WHERE i.indrelid = %s ORDER BY 1, 4, 5",
                (oid,),
            )
        ).fetchall()
        triggers = await (
            await connection.execute(
                "SELECT tgname, tgenabled, replace(pg_get_triggerdef(oid),"
                " quote_ident(current_schema()) || '.', '') FROM pg_trigger"
                " WHERE tgrelid = %s AND NOT tgisinternal ORDER BY tgname",
                (oid,),
            )
        ).fetchall()
        # Keep a separate hash for each layer so diagnosis is local and static,
        # without exposing DDL, tenant data, or arbitrary database diagnostics.
        for kind, value in (
            ("columns", columns),
            ("constraints", constraints),
            ("indexes", indexes),
            ("triggers", triggers),
        ):
            result[f"{name}:{kind}"] = _digest(value)
    functions = await (
        await connection.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosrc,"
            " l.lanname, p.provolatile, p.proisstrict, p.prosecdef, p.proconfig,"
            " pg_get_function_result(p.oid) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang"
            " WHERE n.nspname = current_schema() AND starts_with(p.proname, 'reporting_')"
            " ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)"
        )
    ).fetchall()
    for row in functions:
        result[f"function:{row[0]}({row[1]})"] = _digest(row[2:])
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def validate_schema(connection: Any) -> None:
    installed = await schema_contract(connection)
    if not SCHEMA_CONTRACT or any(
        installed.get(key) != value for key, value in SCHEMA_CONTRACT.items()
    ):
        raise ReportingNotificationError("notification_schema_unready")
