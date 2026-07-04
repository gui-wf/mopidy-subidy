"""Tests for the album smart-lists surfaces (the "Lists" vdir).

These mock libsonic.Connection (reusing the make_api harness from
test_coverart) so they run with no Subsonic server and no GStreamer. They cover
the LIST uri round-trip, the get_album_list_as_refs happy path (real album
refs), the resilience contract (network failure and - critically - a status-ok
response with a null/absent albumList2 container, which must NOT raise), the
config default/knob, the Lists vdir listing, and the browse dispatch including
the invalid/bare ltype guard.
"""

from unittest import mock

from mopidy.models import Ref

from mopidy_subidy import subsonic_api, uri
from mopidy_subidy.library import (
    _ALBUM_LISTS,
    _VALID_LIST_TYPES,
    SubidyLibraryProvider,
)

from tests.test_coverart import make_api


def make_library(api):
    backend = mock.Mock()
    backend.subsonic_api = api
    return SubidyLibraryProvider(backend=backend)


def album(id, name):
    return {"id": id, "name": name}


# --- uri round-trip --------------------------------------------------------


def test_list_uri_round_trip():
    for _, ltype in _ALBUM_LISTS:
        u = uri.get_list_uri(ltype)
        assert uri.get_type(u) == uri.LIST
        assert uri.get_list_type(u) == ltype


def test_bare_list_uri_yields_none():
    assert uri.get_list_type("subidy:list:") is None


def test_list_type_rejects_foreign_and_wrong_type():
    assert uri.get_list_type("spotify:album:xyz") is None
    assert uri.get_list_type(uri.get_album_uri("1")) is None


# --- get_album_list_as_refs happy path -------------------------------------


def test_get_album_list_as_refs_ok_returns_album_refs():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": {"album": [album("1", "X"), album("2", "Y")]},
    }
    api = make_api(connection)

    refs = api.get_album_list_as_refs("random")
    assert all(isinstance(r, Ref) and r.type == Ref.ALBUM for r in refs)
    assert [r.name for r in refs] == ["X", "Y"]
    assert [r.uri for r in refs] == [
        uri.get_album_uri("1"),
        uri.get_album_uri("2"),
    ]
    connection.getAlbumList2.assert_called_once_with(
        ltype="random", size=api.album_list_size, offset=0
    )


# --- resilience: the mustChange defect case --------------------------------


def test_null_album_list_container_returns_empty_never_raises():
    # status ok, but albumList2 is None - realistic for frequent/recent on a
    # server with no play history. Must degrade to [], not AttributeError.
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": None,
    }
    api = make_api(connection)
    assert api.get_album_list_as_refs("frequent") == []


def test_absent_album_list_container_returns_empty():
    # albumList2 key omitted entirely.
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {"status": "ok"}
    api = make_api(connection)
    assert api.get_album_list_as_refs("recent") == []


def test_empty_album_key_returns_empty():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": {"album": None},
    }
    api = make_api(connection)
    assert api.get_album_list_as_refs("highest") == []


def test_network_failure_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getAlbumList2.side_effect = Exception("boom")
    api = make_api(connection)
    assert api.get_album_list_as_refs("newest") == []


def test_non_ok_status_returns_empty():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {"status": "failed"}
    api = make_api(connection)
    assert api.get_album_list_as_refs("newest") == []


# --- config knob -----------------------------------------------------------


def test_album_list_size_honoured():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": {"album": []},
    }
    api = make_api(connection)
    api.album_list_size = 25

    api.get_album_list_as_refs("newest")
    connection.getAlbumList2.assert_called_with(
        ltype="newest", size=25, offset=0
    )


def test_album_list_size_defaults_when_none():
    connection = mock.Mock()
    connection.appName = "subidy"
    connection.apiVersion = "1.16.1"
    with mock.patch("libsonic.Connection", return_value=connection):
        api = subsonic_api.SubsonicApi(
            url="http://example.com",
            username="u",
            password="p",
            app_name="subidy",
            legacy_auth=False,
            api_version="1.16.1",
            album_list_size=None,
        )
    assert api.album_list_size == subsonic_api.ALBUM_LIST_SIZE_DEFAULT


# --- Lists vdir and browse dispatch ----------------------------------------


def test_browse_lists_children_names_and_uris():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_lists()
    assert [r.name for r in refs] == [name for name, _ in _ALBUM_LISTS]
    assert [r.uri for r in refs] == [
        uri.get_list_uri(ltype) for _, ltype in _ALBUM_LISTS
    ]
    assert all(r.type == Ref.DIRECTORY for r in refs)
    # Starred is not duplicated here.
    assert "starred" not in _VALID_LIST_TYPES


def test_browse_lists_vdir_dispatch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri("lists"))
    assert [r.name for r in refs] == [name for name, _ in _ALBUM_LISTS]


def test_lists_appears_in_root_vdir():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri("root"))
    assert "Lists" in [r.name for r in refs]


def test_browse_dispatch_list_returns_album_refs():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": {"album": [album("1", "Newest")]},
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_list_uri("newest"))
    assert [r.name for r in refs] == ["Newest"]
    assert refs[0].uri == uri.get_album_uri("1")
    connection.getAlbumList2.assert_called_once_with(
        ltype="newest", size=api.album_list_size, offset=0
    )


# --- guards: invalid / bare ltype never hit the server ---------------------


def test_invalid_ltype_browse_returns_empty_no_fetch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    assert library.browse("subidy:list:garbage") == []
    connection.getAlbumList2.assert_not_called()


def test_bare_list_uri_browse_returns_empty_no_fetch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    assert library.browse("subidy:list:") == []
    connection.getAlbumList2.assert_not_called()


def test_starred_ltype_rejected_not_duplicated():
    # 'starred' is a valid getAlbumList2 token but deliberately not exposed;
    # a hand-crafted subidy:list:starred must be guarded out.
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    assert library.browse("subidy:list:starred") == []
    connection.getAlbumList2.assert_not_called()
