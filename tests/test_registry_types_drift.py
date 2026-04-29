"""Drift detection: ensure generated registry types match the OpenAPI spec."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "schemas" / "registry-openapi.yaml"


@pytest.mark.skipif(not HAS_YAML, reason="pyyaml not installed")
class TestRegistryTypesDrift:
    """Verify generated types cover the OpenAPI component schemas."""

    @pytest.fixture
    def openapi_schemas(self) -> dict:
        spec = yaml.safe_load(OPENAPI_PATH.read_text())
        return spec.get("components", {}).get("schemas", {})

    @pytest.fixture
    def registry_module(self):
        import adcp.types.registry as reg

        return reg

    def test_all_component_schemas_have_types(self, openapi_schemas: dict, registry_module):
        """Every component/schema in the OpenAPI spec has a generated class."""
        missing = []
        # Get all class names from the registry module
        module_classes = {
            name
            for name, obj in vars(registry_module).items()
            if isinstance(obj, type) and not name.startswith("_")
        }

        for schema_name in openapi_schemas:
            # Check if any class name matches (exact or renamed)
            # We allow renames via the codegen script, so just check
            # that the schema is represented somehow
            found = any(
                schema_name in cls or cls.startswith(schema_name[:5]) for cls in module_classes
            )
            if not found:
                # More lenient: check if a Pydantic model with matching
                # required fields exists
                schema_props = set(openapi_schemas[schema_name].get("properties", {}).keys())
                found = any(
                    hasattr(getattr(registry_module, cls), "model_fields")
                    and schema_props.issubset(
                        set(getattr(registry_module, cls).model_fields.keys())
                        | {"pass_"}  # alias for 'pass' keyword
                    )
                    for cls in module_classes
                    if hasattr(getattr(registry_module, cls, None), "model_fields")
                )
            if not found:
                missing.append(schema_name)

        assert missing == [], (
            f"OpenAPI schemas without generated types: {missing}. "
            "Run: python scripts/generate_registry_types.py"
        )

    def test_all_openapi_endpoints_have_client_methods(self):
        """Every OpenAPI path has a corresponding RegistryClient method."""
        from adcp.registry import RegistryClient

        spec = yaml.safe_load(OPENAPI_PATH.read_text())
        paths = spec.get("paths", {})

        # Map operationIds to expected method names
        client_methods = {
            name
            for name in dir(RegistryClient)
            if not name.startswith("_") and callable(getattr(RegistryClient, name))
        }

        # These are covered by existing methods with different names
        OPERATION_TO_METHOD = {  # noqa: N806
            "resolveBrand": "lookup_brand",
            "resolveBrandsBulk": "lookup_brands",
            "resolveProperty": "lookup_property",
            "resolvePropertiesBulk": "lookup_properties",
            "listMembers": "list_members",
            "getMember": "get_member",
            "listPolicies": "list_policies",
            "resolvePolicy": "resolve_policy",
            "resolvePoliciesBulk": "resolve_policies",
            "getPolicyHistory": "policy_history",
            "savePolicy": "save_policy",
            "getBrandJson": "get_brand_json",
            "saveBrand": "save_brand",
            "listBrands": "list_brands",
            "getBrandHistory": "brand_history",
            "enrichBrand": "enrich_brand",
            "listProperties": "list_properties",
            "validateProperty": "validate_property",
            "saveProperty": "save_property",
            "getPropertyHistory": "property_history",
            "checkPropertyList": "check_property_list",
            "getPropertyCheckReport": "get_property_check_report",
            "listAgents": "list_agents",
            "listPublishers": "list_publishers",
            "getRegistryStats": "get_registry_stats",
            "searchAgentProfiles": "search_agents",
            "requestCrawl": "request_crawl",
            "lookupDomain": "lookup_domain",
            "lookupProperty": "lookup_property_identifier",
            "getAgentDomains": "get_agent_domains",
            "validateProductAuthorization": "validate_product_authorization",
            "expandProductIdentifiers": "expand_product_identifiers",
            "validatePropertyAuthorization": "validate_property_authorization",
            "validateAdagents": "validate_adagents",
            "createAdagents": "create_adagents",
            "apiDiscovery": "api_discovery",
            "search": "search",
            "lookupManifestRef": "lookup_manifest_ref",
            "discoverAgent": "discover_agent",
            "getAgentFormats": "get_agent_formats",
            "getAgentProducts": "get_agent_products",
            "validatePublisher": "validate_publisher",
            "getRegistryFeed": "get_feed",
        }

        missing = []
        for path, methods in paths.items():
            for method, spec_data in methods.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                op_id = spec_data.get("operationId", "")
                expected_method = OPERATION_TO_METHOD.get(op_id)
                if expected_method is None:
                    missing.append(f"{method.upper()} {path} (operationId: {op_id})")
                elif expected_method not in client_methods:
                    missing.append(
                        f"{method.upper()} {path} → {expected_method} (not found on RegistryClient)"
                    )

        assert missing == [], "OpenAPI endpoints without client methods:\n" + "\n".join(
            f"  - {m}" for m in missing
        )
