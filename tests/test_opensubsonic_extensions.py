"""Tests for OpenSubsonic extension negotiation and the supports_extension gate.

These reuse the make_api harness from test_coverart (mocked libsonic.Connection,
no server, no GStreamer). They pin the connection's low-level request path
(_getRequest / _doInfoReq) so negotiation runs against canned payloads:
a Navidrome-shaped response, a no-key response, a JSON error envelope, and a
raising request (classic Subsonic / network failure).
"""

from unittest import mock

from mopidy_subidy import subsonic_api

from tests.test_coverart import make_api

NAVIDROME_PAYLOAD = {
    "status": "ok",
    "openSubsonicExtensions": [
        {"name": "transcodeOffset", "versions": [1]},
        {"name": "formPost", "versions": [1, 2]},
    ],
}


def _api_with_negotiation(doinfo_return=None, doinfo_side_effect=None):
    """Build a SubsonicApi whose negotiation call sees the given payload.

    make_api patches libsonic.Connection to the mock, and SubsonicApi.__init__
    runs _negotiate_opensubsonic_extensions during construction. We pre-wire
    _getRequest/_doInfoReq on the mock so that negotiation is deterministic.
    """
    connection = mock.Mock()
    connection._getRequest.return_value = mock.sentinel.req
    if doinfo_side_effect is not None:
        connection._doInfoReq.side_effect = doinfo_side_effect
    else:
        connection._doInfoReq.return_value = doinfo_return
    return make_api(connection)


def test_navidrome_payload_populates_and_gates():
    api = _api_with_negotiation(doinfo_return=NAVIDROME_PAYLOAD)
    assert api.supports_extension("transcodeOffset") is True
    assert api.supports_extension("transcodeOffset", 1) is True
    assert api.supports_extension("transcodeOffset", 2) is False
    assert api.supports_extension("formPost", 2) is True
    assert api.supports_extension("nope") is False
    assert api.supports_extension("nope", 1) is False


def test_negotiation_called_with_dotview_name():
    api = _api_with_negotiation(doinfo_return=NAVIDROME_PAYLOAD)
    api.connection._getRequest.assert_called_once_with(
        "getOpenSubsonicExtensions.view"
    )


def test_missing_key_yields_no_extensions():
    # JSON error envelope OR a success with no openSubsonicExtensions key:
    # _doInfoReq returns normally, the key is absent, store stays empty.
    api = _api_with_negotiation(doinfo_return={"status": "ok"})
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("transcodeOffset") is False


def test_failed_envelope_yields_no_extensions():
    # Classic/older server answering with a JSON error envelope (HTTP 200,
    # status=failed). _doInfoReq does not inspect status, so this returns a
    # dict with no extensions key -> empty store, no exception.
    api = _api_with_negotiation(
        doinfo_return={"status": "failed", "error": {"code": 0}}
    )
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("anything") is False


def test_raising_request_is_non_fatal():
    # Classic Subsonic / network failure: _doInfoReq raises. Negotiation must
    # swallow it, leave the store empty, and never crash construction.
    api = _api_with_negotiation(doinfo_side_effect=RuntimeError("boom"))
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("transcodeOffset") is False
    assert api.supports_extension("transcodeOffset", 1) is False


def test_bare_int_version_normalized():
    # Some servers advertise versions as a bare int rather than a list,
    # including the falsy 0 case, which must not be dropped.
    payload = {
        "openSubsonicExtensions": [
            {"name": "extBareOne", "versions": 3},
            {"name": "extBareZero", "versions": 0},
        ]
    }
    api = _api_with_negotiation(doinfo_return=payload)
    assert api.supports_extension("extBareOne", 3) is True
    assert api.supports_extension("extBareZero") is True
    assert api.supports_extension("extBareZero", 0) is True


def test_bool_versions_are_excluded():
    # bool is an int subclass; a server sending versions: [true, false] must
    # NOT be stored as {1, 0} (that would make supports_extension(name, 1) lie).
    payload = {
        "openSubsonicExtensions": [
            {"name": "extBool", "versions": [True, False]},
        ]
    }
    api = _api_with_negotiation(doinfo_return=payload)
    assert api.supports_extension("extBool") is True  # advertised at all
    assert api.supports_extension("extBool", 1) is False  # but not version 1
    assert api.supports_extension("extBool", 0) is False


def test_malformed_elements_are_skipped():
    # Nameless entry dropped; non-int versions filtered; a single element
    # served as a bare object rather than an array is coerced by _as_list.
    payload = {
        "openSubsonicExtensions": {
            "name": "solo",
            "versions": ["x", 2, None],
        }
    }
    api = _api_with_negotiation(doinfo_return=payload)
    assert api.supports_extension("solo") is True
    assert api.supports_extension("solo", 2) is True
    assert api.supports_extension("solo", 0) is False


def test_null_subsonic_response_is_non_fatal():
    # _doInfoReq returns dres['subsonic-response'] verbatim; a body of
    # {"subsonic-response": null} yields None. Negotiation must degrade to an
    # empty store rather than raising AttributeError out of __init__ (which
    # would take down backend startup).
    api = _api_with_negotiation(doinfo_return=None)
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("transcodeOffset") is False


def test_non_dict_subsonic_response_is_non_fatal():
    # A non-object envelope (server returns a JSON list/scalar as
    # subsonic-response) must not raise either.
    api = _api_with_negotiation(doinfo_return=["unexpected"])
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("anything") is False


def test_bare_mock_connection_is_non_fatal():
    # The other test modules build SubsonicApi with a bare mock.Mock()
    # connection (no _doInfoReq wiring). Negotiation must not break that path:
    # _doInfoReq auto-returns a Mock, .get returns a Mock, _as_list yields [].
    connection = mock.Mock()
    api = make_api(connection)
    assert api._opensubsonic_extensions == {}
    assert api.supports_extension("transcodeOffset") is False


def test_negotiation_bounds_socket_timeout_and_restores():
    # A no-timeout urllib call could stall startup ~2 min on a network blip.
    # Negotiation must set a bounded socket default timeout during the call and
    # restore the prior value afterwards, even when the request raises.
    import socket

    sentinel = object()
    seen = {}

    def raising_doinfo(_req):
        seen["timeout_during"] = socket.getdefaulttimeout()
        raise Exception("SSL EOF")

    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(None)
    try:
        api = _api_with_negotiation(doinfo_side_effect=raising_doinfo)
        # bounded to the constant during the call...
        assert seen["timeout_during"] == (
            subsonic_api.OPENSUBSONIC_NEGOTIATION_TIMEOUT
        )
        # ...and restored to the prior default afterwards (None here)
        assert socket.getdefaulttimeout() is None
        # failure is non-fatal: no extensions
        assert api.supports_extension("anything") is False
    finally:
        socket.setdefaulttimeout(old)
    _ = sentinel
