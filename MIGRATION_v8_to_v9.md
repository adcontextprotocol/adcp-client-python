# Migrating from SDK 8 to 9

SDK 9 changes validation, server security and reporting ownership. Review the
changes below before upgrading each client, server and reporting worker.

| Change | Update your integration |
| --- | --- |
| Every configured credential carrier is inspected | Send one valid `Authorization: Bearer` token. Remove empty or malformed legacy/proxy headers. Different tokens, or any malformed carrier alongside a valid one, return 401; identical valid duplicates are accepted. See [handler authentication](docs/handler-authoring.md). |
| A2A Host validation defaults to loopback | Configure `allowed_hosts` with your deployment's public host. Unlisted hosts return 421, including on agent-card discovery. See [HTTP transport security](docs/handler-authoring.md#http-host-and-origin-policy). |
| Supplied Origin headers are checked on protocol and operational routes | Configure `allowed_origins` for browser protocol callers. Public GET/HEAD discovery accepts any Origin but still checks Host. Add CORS middleware separately when browsers need to read discovery responses. See [HTTP transport security](docs/handler-authoring.md#http-host-and-origin-policy). |
| Boolean, integer and number validation is strict | Send JSON booleans and numbers rather than strings such as `"yes"` or `"100"`. Integral floats can still validate as integers. Versioned schema validation also checks URI and hostname formats. See [generated type migration](docs/types-9-migration.md#4-strict-scalars-1375). |
| Generated required-field groups are enforced | Supply the fields required by a schema arm: for example, a frequency-cap field group, or packages/proposal for a media-buy request. Empty models that previously constructed can now raise `ValidationError`. See [generated type migration](docs/types-9-migration.md). |
| Scalar schema roots are scalar subclasses | Replace scalar `.model_validate()` with `TypeAdapter` and remove `.root` access. See [scalar roots](docs/types-9-migration.md#1-scalar-roots-are-the-scalar-1286). |
| Union roots are typing aliases; single-model roots are models | Validate union payloads with `validate_union` or a reused `TypeAdapter`. List roots still use `.root`. See [composing roots](docs/types-9-migration.md#2-composing-roots-are-what-they-compose-1353). |
| `ProductFormatDeclaration` names the schema's authoring union | Use `validate_union(ProductFormatDeclaration, payload)` for its typed branches. Use `Format` for open consumer/projection code and `params_as`. See [product declarations](docs/types-9-migration.md#product-declarations-use-the-authoring-union). |
| Structural pointer references share the selected type | Remove imports of obsolete internal per-position wrappers. Use public `adcp.types` names or `adcp.types.domains` modules. See [pointer references](docs/types-9-migration.md#3-pointer-refs-resolve-to-the-selected-type-1371). |
| Format-kind fields preserve open strings | Replace enum identity comparisons with string equality. Opt into `CanonicalFormatKindStr` when your application needs a closed vocabulary. See [format-kind validation](docs/canonical-format-kinds-migration.md). |
| Reporting generations require authenticated caller ownership | Include `consumer_id` in custom store/binding implementations and typed constructors. Migrate populated ledgers with stopped workers; an entirely empty ledger can use the explicit replacement API. See [reporting ownership migration](docs/reporting-caller-ownership-migration.md). |
| New source requests and sealed artifacts include their owning consumer | Read `request.identity.consumer_id` when implementing adapters. Historical ownerless artifacts retain their original hashes, but active owned generations refuse ownerless or differently owned frozen acquisitions; reconcile retained state and admit a new generation instead of relabelling evidence. See [reporting ownership migration](docs/reporting-caller-ownership-migration.md). |
| URL canonicalization gives an empty path its `/` whatever the query does | The `@target-uri` signature base changes for bare-authority URLs such as `https://seller.example.com`. Dial signed clients at the explicit root path, which canonicalizes identically on both versions, or move an unrespellable agent URL's signer and verifier in one window. Identifier matching on `adagents.json`, `brand.json`, TMP and the governance issuer only becomes more permissive. See [the empty-path canonicalization fix](docs/request-signing-migration.md#6-upgrading-across-the-empty-path-canonicalization-fix). |
| Initial automatic source reads default to a deterministic five-minute jitter window | Reads wait for the selected offering's declared availability and then spread within the available delivery headroom. Configure `read_jitter_window=timedelta(0)` to opt out. Pass an explicit window to custom producer factories only when they accept and forward the new keyword. Manual acquisitions, persisted retries and official-close timing retain their behavior. See [reporting service timing](docs/reliable-reporting-service.md). |

The credential rule applies to all configured carriers, including opted-in
legacy header aliases. A valid token in `x-adcp-auth` does not rescue a malformed
`Authorization` header, and a valid Bearer token does not rescue an empty alias.
Audit client and reverse-proxy headers before upgrading. Configure legacy aliases
on `BearerTokenAuth` only when they are needed during your transition.

For reporting, keep admission disabled until every process uses the new API.
Do not rely on an old worker failing an INSERT to make mixed-version readers,
updates or deletes safe. The ownership guide describes the operator-only upgrade
and retained-state recovery paths.

Request signing remains a separately configured protocol capability. Follow the
[request-signing migration guide](docs/request-signing-migration.md) when enabling
or changing signing requirements.

Servers can now opt into a smaller installed protocol surface with
`supported_versions=["3.2"]`. This restricts dispatch and capability advertising
without changing the SDK's default selection. See [served versions](docs/handler-authoring.md#select-served-protocol-versions).
