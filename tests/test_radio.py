"""Tests for algorithmic radio / instant-mix surfaces.

These mock libsonic.Connection (reusing the make_api harness from
test_coverart) so they run with no Subsonic server and no GStreamer. They
cover the raw fetchers and ref builders for Random / Similar / Top songs,
their resilience (network failure, empty, missing-key, single-object-not-a-
list), the single-getArtist artist browse (no duplicate round-trip), the
per-album Similar entry, and the SIMILAR/TOP uri round-trip.
"""

from unittest import mock

from mopidy.models import Ref

from mopidy_subidy import subsonic_api, uri
from mopidy_subidy.library import SubidyLibraryProvider

from tests.test_coverart import make_api


def make_library(api):
    backend = mock.Mock()
    backend.subsonic_api = api
    return SubidyLibraryProvider(backend=backend)


def song(id, title):
    return {"id": id, "title": title}


# --- uri round-trip --------------------------------------------------------


def test_similar_uri_round_trip():
    u = uri.get_similar_uri("ar-123")
    assert uri.get_type(u) == uri.SIMILAR
    assert uri.get_similar_id(u) == "ar-123"


def test_top_uri_round_trip_preserves_colon_space_slash_percent():
    # No quoting: the module regex captures the name verbatim.
    for name in [
        "Godspeed You! Black Emperor",
        "Sun Ra: The Arkestra",
        "AC/DC",
        "100% Silk",
        "a:b:c",
    ]:
        u = uri.get_top_uri(name)
        assert uri.get_type(u) == uri.TOP
        assert uri.get_top_id(u) == name


def test_similar_id_rejects_foreign_and_wrong_type():
    assert uri.get_similar_id("spotify:track:xyz") is None
    assert uri.get_similar_id(uri.get_top_uri("Foo")) is None
    assert uri.get_top_id(uri.get_similar_uri("1")) is None


# --- similar songs ---------------------------------------------------------


def test_get_similar_songs_as_refs_ok():
    connection = mock.Mock()
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {"song": [song("1", "A"), song("2", "B")]},
    }
    api = make_api(connection)

    refs = api.get_similar_songs_as_refs("ar-1")
    assert [r.name for r in refs] == ["A", "B"]
    assert all(isinstance(r, Ref) and r.type == Ref.TRACK for r in refs)
    assert [r.uri for r in refs] == [
        uri.get_song_uri("1"),
        uri.get_song_uri("2"),
    ]
    connection.getSimilarSongs2.assert_called_once_with("ar-1", api.radio_size)


def test_get_similar_songs_single_object_coerced_to_list():
    connection = mock.Mock()
    # A single similar song emitted as a bare dict, not a one-element array.
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {"song": song("9", "Solo")},
    }
    api = make_api(connection)

    refs = api.get_similar_songs_as_refs("al-1")
    assert [r.name for r in refs] == ["Solo"]


def test_get_similar_songs_empty_and_missing_key():
    connection = mock.Mock()
    api = make_api(connection)

    # song key absent
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {},
    }
    assert api.get_similar_songs_as_refs("x") == []

    # similarSongs2 container absent entirely
    connection.getSimilarSongs2.return_value = {"status": "ok"}
    assert api.get_similar_songs_as_refs("x") == []

    # explicit None song list
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {"song": None},
    }
    assert api.get_similar_songs_as_refs("x") == []


def test_get_similar_songs_network_failure_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getSimilarSongs2.side_effect = Exception("boom")
    api = make_api(connection)

    assert api.get_similar_songs_as_refs("x") == []


# --- top songs -------------------------------------------------------------


def test_get_top_songs_as_refs_ok_passes_name_not_id():
    connection = mock.Mock()
    connection.getTopSongs.return_value = {
        "status": "ok",
        "topSongs": {"song": [song("1", "Hit")]},
    }
    api = make_api(connection)

    refs = api.get_top_songs_as_refs("Radiohead")
    assert [r.name for r in refs] == ["Hit"]
    # getTopSongs takes the artist NAME string, not an id.
    connection.getTopSongs.assert_called_once_with(
        "Radiohead", api.radio_size
    )


def test_get_top_songs_single_object_and_empty():
    connection = mock.Mock()
    api = make_api(connection)

    connection.getTopSongs.return_value = {
        "status": "ok",
        "topSongs": {"song": song("3", "Only")},
    }
    assert [r.name for r in api.get_top_songs_as_refs("X")] == ["Only"]

    connection.getTopSongs.return_value = {"status": "ok", "topSongs": {}}
    assert api.get_top_songs_as_refs("X") == []


def test_get_top_songs_network_failure_returns_empty():
    connection = mock.Mock()
    connection.getTopSongs.side_effect = Exception("boom")
    api = make_api(connection)

    assert api.get_top_songs_as_refs("X") == []


# --- random size honours config -------------------------------------------


def test_random_and_radio_share_radio_size():
    connection = mock.Mock()
    connection.getRandomSongs.return_value = {
        "status": "ok",
        "randomSongs": {"song": [song("1", "R")]},
    }
    api = make_api(connection)
    api.radio_size = 7

    api.get_random_songs_as_refs()
    connection.getRandomSongs.assert_called_with(7)


def test_radio_size_defaults_when_none():
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
            radio_size=None,
        )
    assert api.radio_size == subsonic_api.RADIO_SIZE_DEFAULT


# --- artist browse: single getArtist, radio entries first ------------------


def test_browse_artist_single_getartist_call_name_and_albums():
    connection = mock.Mock()
    connection.getArtist.return_value = {
        "status": "ok",
        "artist": {
            "id": "ar-1",
            "name": "Portishead",
            "album": [{"id": "al-1", "name": "Dummy"}],
        },
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_artist("ar-1")

    # Exactly ONE getArtist round-trip for the whole artist browse.
    assert connection.getArtist.call_count == 1
    names = [r.name for r in refs]
    # Radio entries lead, deterministically, then the album.
    assert names == ["Similar Songs", "Top Songs", "Dummy"]
    assert refs[0].uri == uri.get_similar_uri("ar-1")
    assert refs[1].uri == uri.get_top_uri("Portishead")
    assert refs[0].type == Ref.DIRECTORY and refs[1].type == Ref.DIRECTORY


def test_browse_artist_missing_name_skips_top_songs():
    connection = mock.Mock()
    connection.getArtist.return_value = {
        "status": "ok",
        "artist": {"id": "ar-1", "album": []},
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_artist("ar-1")
    names = [r.name for r in refs]
    # No name -> Top Songs skipped; Similar (id-based) still present.
    assert names == ["Similar Songs"]


def test_browse_artist_getartist_failure_still_shows_similar():
    connection = mock.Mock()
    connection.getArtist.side_effect = Exception("boom")
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_artist("ar-1")
    assert [r.name for r in refs] == ["Similar Songs"]


# --- album browse: Similar entry leads, then songs -------------------------


def test_browse_album_prepends_similar_entry():
    connection = mock.Mock()
    connection.getAlbum.return_value = {
        "status": "ok",
        "album": {"id": "al-1", "song": [song("1", "Track1")]},
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_album("al-1")
    assert refs[0].name == "Similar Songs"
    assert refs[0].uri == uri.get_similar_uri("al-1")
    assert refs[0].type == Ref.DIRECTORY
    assert [r.name for r in refs[1:]] == ["Track1"]


# --- radio vdir and browse dispatch ----------------------------------------


def test_browse_radio_lists_random_songs_child():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse_radio()
    assert [r.name for r in refs] == ["Random Songs"]
    assert refs[0].uri == uri.get_vdir_uri("random")


def test_browse_dispatch_similar_and_top():
    connection = mock.Mock()
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {"song": [song("1", "S")]},
    }
    connection.getTopSongs.return_value = {
        "status": "ok",
        "topSongs": {"song": [song("2", "T")]},
    }
    api = make_api(connection)
    library = make_library(api)

    similar = library.browse(uri.get_similar_uri("ar-1"))
    assert [r.name for r in similar] == ["S"]

    top = library.browse(uri.get_top_uri("Björk"))
    assert [r.name for r in top] == ["T"]
    connection.getTopSongs.assert_called_once_with("Björk", api.radio_size)


# --- hardening: None payloads, missing id, bare uris -----------------------


def test_get_raw_songs_none_album_payload_returns_empty():
    # getAlbum returns status ok but a None album payload.
    connection = mock.Mock()
    connection.getAlbum.return_value = {"status": "ok", "album": None}
    api = make_api(connection)
    assert api.get_raw_songs("al-1") == []
    assert api.get_songs_as_refs("al-1") == []


def test_get_raw_random_song_none_payload_returns_empty():
    connection = mock.Mock()
    connection.getRandomSongs.return_value = {
        "status": "ok",
        "randomSongs": None,
    }
    api = make_api(connection)
    assert api.get_raw_random_song(5) == []
    assert api.get_random_songs_as_refs() == []


def test_similar_and_top_none_container_returns_empty():
    connection = mock.Mock()
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": None,
    }
    connection.getTopSongs.return_value = {"status": "ok", "topSongs": None}
    api = make_api(connection)
    assert api.get_similar_songs_as_refs("x") == []
    assert api.get_top_songs_as_refs("X") == []


def test_song_without_id_is_skipped_no_none_ref():
    connection = mock.Mock()
    connection.getSimilarSongs2.return_value = {
        "status": "ok",
        "similarSongs2": {
            "song": [{"title": "NoId"}, song("2", "HasId")]
        },
    }
    api = make_api(connection)
    refs = api.get_similar_songs_as_refs("x")
    # The id-less song is dropped; no 'subidy:song:None' ref appears.
    assert [r.name for r in refs] == ["HasId"]
    assert all(r.uri != uri.get_song_uri(None) for r in refs)
    assert "subidy:song:None" not in [r.uri for r in refs]


def test_song_without_id_skipped_in_random_and_top():
    connection = mock.Mock()
    connection.getRandomSongs.return_value = {
        "status": "ok",
        "randomSongs": {"song": [{"title": "Ghost"}]},
    }
    connection.getTopSongs.return_value = {
        "status": "ok",
        "topSongs": {"song": [{"title": "Ghost"}]},
    }
    api = make_api(connection)
    assert api.get_random_songs_as_refs() == []
    assert api.get_top_songs_as_refs("X") == []


def test_raw_song_to_track_without_id_returns_none():
    connection = mock.Mock()
    api = make_api(connection)
    assert api.raw_song_to_track({"title": "NoId"}) is None
    assert api.raw_song_to_ref({"title": "NoId"}) is None


def test_bare_similar_uri_browse_returns_empty_no_fetch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)
    assert library.browse("subidy:similar:") == []
    connection.getSimilarSongs2.assert_not_called()


def test_bare_top_uri_browse_returns_empty_no_fetch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)
    assert library.browse("subidy:top:") == []
    connection.getTopSongs.assert_not_called()


def test_lookup_of_similar_dir_returns_empty_list():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)
    # A directory-only radio uri has no track lookup; must be a list, not None.
    assert library.lookup(uri=uri.get_similar_uri("ar-1")) == []
    assert library.lookup(uri=uri.get_top_uri("Foo")) == []
    res = library.lookup(uris=[uri.get_similar_uri("ar-1")])
    assert res == {uri.get_similar_uri("ar-1"): []}
