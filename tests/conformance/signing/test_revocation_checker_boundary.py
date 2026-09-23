"""Public verifier classification and live signed-list receiver composition.

The live test uses real loopback HTTP for both the issuer and callback. Its
injected wall/monotonic clocks exercise the documented cache grace interval;
callback signature verification and revocation-list JWS verification are
observed separately and still execute their real cryptography.
"""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import (
    CachingRevocationChecker,
    InMemoryReplayStore,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    verify_request_signature,
)
from adcp.signing.revocation_fetcher import (
    FetchResult,
    RevocationListFetchError,
    RevocationListFreshnessError,
    RevocationListParseError,
    RevocationListSignatureError,
)
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    create_mcp_webhook_payload,
    sign_webhook,
    to_wire_dict,
    verify_webhook_signature,
)

from .test_revocation_e2e import _make_operator_key, _make_signer_key, _sign_revocation_list

NOW = 1_776_520_800
URL = "https://buyer.example.test/webhook"
ROUTES = ("3.0", "3.1", "3.2", "webhook")
MAPPED_ERRORS = (
    RevocationListFetchError,
    RevocationListParseError,
    RevocationListSignatureError,
    RevocationListFreshnessError,
)


def _signed_call(route, checker, resolver, replay, private, kid):
    body = b'{"test":"revocation classification"}'
    common = dict(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private,
        key_id=kid,
        alg="ed25519",
        created=NOW,
        nonce="revocation-boundary-nonce",
    )
    if route == "webhook":
        signed = sign_webhook(**common)
        options = WebhookVerifyOptions(
            jwks_resolver=resolver,
            revocation_checker=checker,
            replay_store=replay,
            clock=lambda: NOW,
        )
        verifier = verify_webhook_signature
    else:
        signed = sign_request(**common, signing_profile_version=route, cover_content_digest=True)
        options = VerifyOptions(
            now=NOW,
            capability=VerifierCapability(covers_content_digest="required"),
            operation="test",
            jwks_resolver=resolver,
            revocation_checker=checker,
            replay_store=replay,
            signing_profile_version=route,
        )
        verifier = verify_request_signature

    def run():
        return verifier(
            method="POST",
            url=URL,
            headers={"Content-Type": "application/json", **signed.as_dict()},
            body=body,
            options=options,
        )

    return run


def _observe_crypto_and_replay(monkeypatch, replay, events):
    from adcp.signing import verifier

    original = verifier.verify_signature

    def verify(**kwargs):
        events.append("callback_crypto")
        return original(**kwargs)

    monkeypatch.setattr(verifier, "verify_signature", verify)
    for name in ("seen", "remember", "claim", "at_capacity"):
        method = getattr(replay, name)

        def observed(*args, _method=method, _name=name, **kwargs):
            events.append("replay_" + _name)
            return _method(*args, **kwargs)

        monkeypatch.setattr(replay, name, observed)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("error_type", MAPPED_ERRORS)
def test_checker_failure_is_safe_step_nine_and_does_not_claim_nonce(route, error_type, monkeypatch):
    private, jwk = _make_signer_key()
    events = []
    failing = True
    replay = InMemoryReplayStore()
    _observe_crypto_and_replay(monkeypatch, replay, events)

    def resolve(kid):
        events.append("jwks")
        assert kid == jwk["kid"]
        return jwk

    def check(kid):
        events.append("checker")
        assert kid == jwk["kid"]
        if failing:
            raise error_type(
                "private fixture detail: https://issuer.example.test/untrusted-payload"
            )
        return False

    run = _signed_call(route, check, resolve, replay, private, jwk["kid"])
    with pytest.raises(SignatureVerificationError) as raised:
        run()
    prefix = "webhook" if route == "webhook" else "request"
    assert (raised.value.code, raised.value.step) == (prefix + "_signature_revocation_stale", 9)
    assert raised.value.detail is None
    assert "private fixture" not in str(raised.value)
    assert "issuer.example.test" not in str(raised.value)
    assert "untrusted-payload" not in str(raised.value)
    rendered = "".join(traceback.format_exception(raised.value))
    assert "issuer.example.test" not in rendered and "private fixture detail" not in rendered
    assert events == ["jwks", "checker"]

    # The identical signed request remains usable after refresh: stale rejection
    # must not consume its nonce. A subsequent genuine replay must be rejected.
    failing = False
    assert run().key_id == jwk["kid"]
    assert events.count("checker") == 2
    assert events.count("callback_crypto") == 1
    assert "replay_claim" in events
    with pytest.raises(SignatureVerificationError) as replayed:
        run()
    assert replayed.value.code == prefix + "_signature_replayed"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("error_type", (RuntimeError, ValueError, asyncio.CancelledError))
def test_unrelated_checker_errors_remain_operational(route, error_type, monkeypatch):
    private, jwk = _make_signer_key()
    error = error_type("adopter checker error")
    events = []
    replay = InMemoryReplayStore()
    _observe_crypto_and_replay(monkeypatch, replay, events)

    def check(_kid):
        events.append("checker")
        raise error

    run = _signed_call(route, check, lambda _kid: jwk, replay, private, jwk["kid"])
    with pytest.raises(error_type) as raised:
        run()
    assert raised.value is error
    assert events == ["checker"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("revoked", (False, True))
def test_boolean_revocation_controls_keep_their_order(route, revoked, monkeypatch):
    private, jwk = _make_signer_key()
    events = []
    replay = InMemoryReplayStore()
    _observe_crypto_and_replay(monkeypatch, replay, events)

    def check(_kid):
        events.append("checker")
        return revoked

    run = _signed_call(route, check, lambda _kid: jwk, replay, private, jwk["kid"])
    if revoked:
        with pytest.raises(SignatureVerificationError) as raised:
            run()
        prefix = "webhook" if route == "webhook" else "request"
        assert (raised.value.code, raised.value.step) == (prefix + "_signature_key_revoked", 9)
        assert events == ["checker"]
    else:
        assert run().key_id == jwk["kid"]
        assert events.count("checker") == events.count("callback_crypto") == 1
        assert "replay_claim" in events


@contextmanager
def _http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _reply(handler, status, body, headers=()):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _post(url, body, headers):
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        response = urlopen(request, timeout=5)  # noqa: S310 - ephemeral loopback fixture
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), json.loads(response.read())


def test_live_signed_issuer_grace_stale_rejection_and_recovery(monkeypatch):
    from adcp.signing import jws

    operator, operator_jwk = _make_operator_key()
    private, signer_jwk = _make_signer_key()
    state = {"wall": NOW, "mono": 0, "outage": False, "refreshed": False}
    events = []
    fetches = []
    handled = []
    callback_bodies = []
    operational = []
    replay = InMemoryReplayStore()
    _observe_crypto_and_replay(monkeypatch, replay, events)
    original_jws_verify = jws.verify_signature

    def verify_list(**kwargs):
        events.append("list_jws_crypto")
        return original_jws_verify(**kwargs)

    monkeypatch.setattr(jws, "verify_signature", verify_list)

    class Issuer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            assert self.path == "/revocations"
            fetches.append((state["wall"], self.headers.get("If-None-Match")))
            if state["outage"]:
                _reply(self, 503, b"unavailable")
                return
            updated, following = (480, 660) if state["refreshed"] else (-60, 60)
            payload = {
                "version": 1,
                "issuer": "https://issuer.example.test",
                "updated": datetime.fromtimestamp(NOW + updated, timezone.utc).isoformat(),
                "next_update": datetime.fromtimestamp(NOW + following, timezone.utc).isoformat(),
                "revoked_kids": [],
                "revoked_jtis": [],
            }
            body = _sign_revocation_list(
                operator_key=operator, operator_kid=operator_jwk["kid"], payload=payload
            ).encode()
            _reply(self, 200, body, (("ETag", '"signed-list"'),))

    with _http_server(Issuer) as issuer_url:

        def fetch(uri, *, if_none_match=None, if_modified_since=None):
            headers = {} if if_none_match is None else {"If-None-Match": if_none_match}
            try:
                with urlopen(Request(uri, headers=headers), timeout=5) as response:  # noqa: S310
                    return FetchResult(response.read().decode(), response.headers.get("ETag"))
            except (URLError, TimeoutError) as error:
                raise RevocationListFetchError("untrusted fixture transport detail") from error

        checker = CachingRevocationChecker(
            revocation_uri=issuer_url + "/revocations",
            issuer="https://issuer.example.test",
            jwks_resolver=StaticJwksResolver({"keys": [operator_jwk]}),
            fetcher=fetch,
            clock=lambda: state["mono"],
            wall_clock=lambda: datetime.fromtimestamp(state["wall"], timezone.utc),
        )

        def check(kid):
            events.append("checker")
            return checker(kid)

        def resolve(kid):
            events.append("callback_jwks")
            return signer_jwk if kid == signer_jwk["kid"] else None

        receiver = WebhookReceiver(
            WebhookReceiverConfig(
                verify_options=WebhookVerifyOptions(
                    jwks_resolver=resolve,
                    revocation_checker=check,
                    replay_store=replay,
                    clock=lambda: NOW,
                ),
                dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
                receiver_scope="live-revocation-test",
                publisher_scope_for=lambda _sender: "fixture-publisher",
            )
        )

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                callback_bodies.append(body)
                try:
                    outcome = receiver.receive_and_process_sync(
                        method="POST",
                        url=callback_url + "/webhook",
                        headers=dict(self.headers),
                        body=body,
                        handler=lambda payload: handled.append(payload.idempotency_key),
                    )
                except Exception as error:  # retain operational errors separately from SDK outcomes
                    operational.append(type(error).__name__)
                    _reply(self, 503, json.dumps({"operational": type(error).__name__}).encode())
                    return
                _reply(
                    self,
                    outcome.http_status or 200,
                    json.dumps({"handled": outcome.handled, "rejected": outcome.rejected}).encode(),
                    outcome.response_headers.items(),
                )

        with _http_server(Callback) as callback_url:

            def signed(index):
                payload = create_mcp_webhook_payload(
                    task_id=f"live-list-{index}",
                    task_type="sync_reporting_status",
                    operation_id=f"live-list-operation-{index}",
                    status="completed",
                    idempotency_key=f"whk_revocation_list_fixture_{index:04d}",
                )
                body = json.dumps(to_wire_dict(payload), ensure_ascii=False, indent=1).encode()
                signed_headers = sign_webhook(
                    method="POST",
                    url=callback_url + "/webhook",
                    headers={"Content-Type": "application/json"},
                    body=body,
                    private_key=private,
                    key_id=signer_jwk["kid"],
                    alg="ed25519",
                    created=NOW,
                    nonce=f"live-list-nonce-{index:04d}",
                )
                return body, {"Content-Type": "application/json", **signed_headers.as_dict()}

            def deliver(sample):
                response = _post(callback_url + "/webhook", *sample)
                assert callback_bodies[-1] == sample[0]
                return response

            assert deliver(signed(1))[0] == 200
            assert len(fetches) == 1 and len(handled) == 1
            assert events.count("list_jws_crypto") == events.count("callback_crypto") == 1
            assert deliver(signed(2))[0] == 200  # fresh cache: no second fetch
            assert len(fetches) == 1 and len(handled) == 2
            state.update(wall=NOW + 100, mono=60, outage=True)
            assert deliver(signed(3))[0] == 200  # outage within signed interval + 2x grace
            assert len(fetches) == 2 and len(handled) == 3
            state.update(wall=NOW + 500, mono=120)
            sample = signed(4)
            before = len(events)
            stale = deliver(sample)
            assert events[before:] == ["callback_jwks", "checker"]
            assert len(fetches) == 3 and len(handled) == 3
            # Continue recovery even on the old implementation, retaining the
            # full causal sequence before asserting the required classification.
            state.update(wall=NOW + 500, mono=240, outage=False, refreshed=True)
            recovered = deliver(sample)
            assert recovered[0] == 200 and recovered[2]["handled"]
            assert len(fetches) == 4 and len(handled) == 4
            assert events.count("list_jws_crypto") == 2
            assert events.count("callback_crypto") == 4
            assert events.count("checker") == 5
            assert fetches[0][1] is None and all(etag == '"signed-list"' for _, etag in fetches[1:])
            assert stale[0] == 401, {"stale": stale, "operational": operational}
            assert (
                stale[1]["WWW-Authenticate"]
                == 'Signature error="webhook_signature_revocation_stale"'
            )
            assert stale[2] == {"handled": False, "rejected": True}
            assert not operational
