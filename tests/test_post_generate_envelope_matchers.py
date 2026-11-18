"""The post-generation passes find a generated class by name, not by its bases (#1680).

adcp#8547 makes ``core/protocol-envelope.json`` compose
``core/version-envelope.json`` and every task response compose only the
protocol envelope, so the generated response classes change from
``class X(AdcpVersionEnvelope, ProtocolEnvelope):`` to ``class
X(ProtocolEnvelope):``. ``scripts/post_generate_fixes.py`` matched the full
class-signature string in 29 places: one raised ``ListCreativesResponse
marker missing`` on the new shape and the others would have skipped silently.
Every pass now locates its class by name and keeps whatever bases the
generator wrote, the classes this script emits itself derive their envelope
bases from the generated ``ProtocolEnvelope``, and a missing class is an
error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import post_generate_fixes as fixes

OLD = "class ProtocolEnvelope(AdCPBaseModel):\n    pass\n"
NEW = "class ProtocolEnvelope(AdcpVersionEnvelope, AdCPBaseModel):\n    pass\n"


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "domains"
    (directory / "core").mkdir(parents=True)
    (directory / "governance").mkdir()
    (directory / "creative").mkdir()
    (directory / "media_buy").mkdir()
    (directory / "brand").mkdir()
    (directory / "trusted_match").mkdir()
    monkeypatch.setattr(fixes, "OUTPUT_DIR", directory)
    return directory


def _protocol_envelope(generated: Path, composition: str) -> None:
    (generated / "core" / "protocol_envelope.py").write_text(
        "from adcp.types.base import AdCPBaseModel\n\n\n" + composition
    )


def test_find_class_header_ignores_the_base_list() -> None:
    for bases in ("AdcpVersionEnvelope, ProtocolEnvelope", "ProtocolEnvelope", "A, B, C"):
        source = f"x = 1\n\n\nclass Thing({bases}):\n    pass\n"
        match = fixes.find_class_header(source, "Thing")
        assert match is not None
        assert match.group("bases") == bases
    assert fixes.find_class_header("class Other(A):\n    pass\n", "Thing") is None
    with pytest.raises(RuntimeError, match="class Thing is not in the generated module"):
        fixes.require_class_header("class Other(A):\n    pass\n", "Thing", "somewhere")


@pytest.mark.parametrize(
    "composition,protocol_bases",
    [(OLD, "AdcpVersionEnvelope, ProtocolEnvelope"), (NEW, "ProtocolEnvelope")],
    ids=["pre-8547", "post-8547"],
)
def test_emitted_envelope_bases_follow_the_generated_protocol_envelope(
    generated: Path, composition: str, protocol_bases: str
) -> None:
    _protocol_envelope(generated, composition)
    assert fixes.response_envelope_bases(protocol=True) == protocol_bases
    assert fixes.response_envelope_bases(protocol=False) == "AdcpVersionEnvelope"
    module = (
        "from ..core.protocol_envelope import ProtocolEnvelope\n"
        "from ..core.version_envelope import AdcpVersionEnvelope\n\n\n"
        "class R(AdcpVersionEnvelope, ProtocolEnvelope):\n    pass\n"
    )
    normalized = fixes.normalize_emitted_envelope_bases(module)
    assert f"class R({protocol_bases}):" in normalized
    assert ("AdcpVersionEnvelope" in normalized) is (composition is OLD)


def test_envelope_bases_default_to_the_old_shape_without_a_generated_tree(generated: Path) -> None:
    assert fixes.response_envelope_bases(protocol=True) == "AdcpVersionEnvelope, ProtocolEnvelope"


@pytest.mark.parametrize("bases", ["AdcpVersionEnvelope", "ProtocolEnvelope"])
def test_status_alias_passes_find_their_class_whatever_its_bases(
    generated: Path, bases: str
) -> None:
    for module, cls, fix, alias in (
        (
            "check_governance_response",
            "CheckGovernanceResponse",
            fixes.fix_check_governance_status_alias,
            "verdict",
        ),
        (
            "report_plan_outcome_response",
            "ReportPlanOutcomeResponse",
            fixes.fix_report_plan_outcome_status_alias,
            "outcome_state",
        ),
    ):
        target = generated / "governance" / f"{module}.py"
        target.write_text(
            "from typing import Any\n\n"
            "from pydantic import AwareDatetime, ConfigDict, Field\n\n\n"
            f"class {cls}({bases}):\n    status: str | None = None\n"
        )
        fix()
        source = target.read_text()
        assert f"class {cls}({bases}):" in source
        assert f"data['{alias}'] = data['status']" in source
        assert "model_validator" in source


def test_status_alias_pass_fails_loudly_when_the_class_is_gone(generated: Path) -> None:
    target = generated / "governance" / "check_governance_response.py"
    target.write_text(
        "from pydantic import AwareDatetime, ConfigDict, Field\n\n\nclass Other(A):\n    pass\n"
    )
    with pytest.raises(
        RuntimeError, match="CheckGovernanceResponse is not in the generated module"
    ):
        fixes.fix_check_governance_status_alias()


@pytest.mark.parametrize("bases", ["AdcpVersionEnvelope", "ProtocolEnvelope"])
def test_get_products_compatibility_enum_anchors_on_the_class_name(
    generated: Path, bases: str
) -> None:
    (generated / "media_buy" / "product_fields.py").write_text(
        "from adcp.types._str_enum import StrEnum\n\n\n"
        "class ProductResponseField(StrEnum):\n    product_id = 'product_id'\n"
    )
    target = generated / "media_buy" / "get_products_request.py"
    target.write_text(
        "from adcp.types._str_enum import StrEnum\n\n\n"
        "class Fields(StrEnum):\n    brief = 'brief'\n\n\n"
        f"class GetProductsRequest({bases}):\n    fields: list[Fields] | None = None\n"
    )
    fixes.restore_get_products_field_compatibility_enum()
    source = target.read_text()
    assert "class Field1(StrEnum):" in source
    assert f"class GetProductsRequest({bases}):" in source


def test_identity_match_constant_anchors_on_either_envelope_import() -> None:
    old = "from pydantic import Field\n\nfrom ..core.version_envelope import AdcpVersionEnvelope\n"
    new = "from pydantic import Field\n\nfrom ..core.protocol_envelope import ProtocolEnvelope\n"
    assert fixes._envelope_import_anchor(old).startswith("from ..core.version_envelope")
    assert fixes._envelope_import_anchor(new).startswith("from ..core.protocol_envelope")
    with pytest.raises(RuntimeError, match="imports no envelope class"):
        fixes._envelope_import_anchor("from pydantic import Field\n")


def test_no_pass_matches_a_full_response_signature_literal() -> None:
    """The remaining literal spellings are inside the modules this script emits itself."""
    source = Path(fixes.__file__).read_text()
    offenders = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if "AdcpVersionEnvelope, ProtocolEnvelope" not in stripped:
            continue
        # A marker is a quoted ``class`` header used to search generated text.
        if stripped.startswith(('"', "'", 'f"', "marker", '+ "')) and "class " in stripped:
            offenders.append(number)
    assert offenders == [], f"exact class-signature markers remain at lines {offenders}"
