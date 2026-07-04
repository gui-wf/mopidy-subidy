"""Tests for the genre-browsing surfaces (the "Genres" vdir).

These mock libsonic.Connection (reusing the make_api harness from
test_coverart) so they run with no Subsonic server and no GStreamer. They cover
the GENRE uri round-trip for special-character genre names, the
get_genres_as_refs happy path and the value/name/whitespace/empty skipping,
get_songs_by_genre_as_refs (real playable song refs, id-less filtering), the
resilience contract (network failure and a status-ok response with a
null/absent songsByGenre/genres container, which must NOT raise), the config
default/knob, the Genres vdir listing, and the browse dispatch including the
bare-uri guard.
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


def genre(value, song_count=1):
    return {"value": value, "songCount": song_count, "albumCount": song_count}


def song(id, title):
    return {"id": id, "title": title}


SPECIAL_NAMES = [
    "Drum & Bass",
    "Hip-Hop/Rap",
    "Rock: Progressive",
    "100% Electronic",
    "Jazz",
]


# --- uri round-trip --------------------------------------------------------


def test_genre_uri_round_trip_special_chars():
    for name in SPECIAL_NAMES:
        u = uri.get_genre_uri(name)
        assert uri.get_type(u) == uri.GENRE
        assert uri.get_genre_name(u) == name


def test_bare_genre_uri_yields_none():
    assert uri.get_genre_name("subidy:genre:") is None


def test_genre_name_rejects_foreign_and_wrong_type():
    assert uri.get_genre_name("spotify:album:xyz") is None
    assert uri.get_genre_name(uri.get_album_uri("1")) is None


def test_genre_name_with_newline_degrades_to_none():
    # The module regex '.' does not span newlines (no DOTALL), so a genre name
    # containing a literal newline fails to match and returns None rather than
    # raising - documented intended behavior, consistent with TOP.
    assert uri.get_genre_name("subidy:genre:Rock\nRoll") is None


# --- get_genres_as_refs ----------------------------------------------------


def test_get_genres_as_refs_ok_builds_directory_refs():
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": [genre("Rock"), genre("Drum & Bass")]},
    }
    api = make_api(connection)

    refs = api.get_genres_as_refs()
    assert all(isinstance(r, Ref) and r.type == Ref.DIRECTORY for r in refs)
    assert [r.name for r in refs] == ["Rock", "Drum & Bass"]
    assert [r.uri for r in refs] == [
        uri.get_genre_uri("Rock"),
        uri.get_genre_uri("Drum & Bass"),
    ]


def test_get_genres_as_refs_name_key_fallback():
    # Some server variants key the name as 'name' instead of 'value'.
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": [{"name": "Blues", "songCount": 3}]},
    }
    api = make_api(connection)

    refs = api.get_genres_as_refs()
    assert [r.name for r in refs] == ["Blues"]
    assert refs[0].uri == uri.get_genre_uri("Blues")


def test_get_genres_as_refs_skips_empty_and_whitespace_values():
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {
            "genre": [
                genre("Rock"),
                {"value": "", "songCount": 0},
                {"value": "   ", "songCount": 0},
                {"songCount": 0},
            ]
        },
    }
    api = make_api(connection)

    refs = api.get_genres_as_refs()
    # Only the real, non-empty, non-whitespace genre survives.
    assert [r.name for r in refs] == ["Rock"]


def test_get_genres_as_refs_skips_non_string_value():
    # A malformed server emitting a numeric/non-string value must not crash
    # (the name guard is isinstance str, not a bare .strip()).
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": [{"value": 123, "songCount": 1}, genre("Jazz")]},
    }
    api = make_api(connection)
    assert [r.name for r in api.get_genres_as_refs()] == ["Jazz"]


def test_get_genres_as_refs_preserves_exact_name_in_uri():
    # The name must be used verbatim (not stripped) so getSongsByGenre matches
    # the exact server genre string.
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": genre("Drum & Bass")},
    }
    api = make_api(connection)
    ref = api.get_genres_as_refs()[0]
    assert ref.name == "Drum & Bass"
    assert uri.get_genre_name(ref.uri) == "Drum & Bass"


def test_get_genres_as_refs_single_bare_object_coerced():
    # A server emitting a single genre as a bare object (not a list) must be
    # coerced by _as_list, not iterated as dict keys.
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": genre("Solo Genre")},
    }
    api = make_api(connection)

    refs = api.get_genres_as_refs()
    assert [r.name for r in refs] == ["Solo Genre"]


# --- get_raw_genres resilience ---------------------------------------------


def test_genres_network_failure_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getGenres.side_effect = Exception("boom")
    api = make_api(connection)
    assert api.get_genres_as_refs() == []


def test_genres_non_ok_status_returns_empty():
    connection = mock.Mock()
    connection.getGenres.return_value = {"status": "failed"}
    api = make_api(connection)
    assert api.get_genres_as_refs() == []


def test_null_genres_container_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getGenres.return_value = {"status": "ok", "genres": None}
    api = make_api(connection)
    assert api.get_genres_as_refs() == []


def test_absent_genres_container_returns_empty():
    connection = mock.Mock()
    connection.getGenres.return_value = {"status": "ok"}
    api = make_api(connection)
    assert api.get_genres_as_refs() == []


def test_empty_genre_key_returns_empty():
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": None},
    }
    api = make_api(connection)
    assert api.get_genres_as_refs() == []


# --- get_songs_by_genre_as_refs --------------------------------------------


def test_get_songs_by_genre_as_refs_ok_returns_song_refs():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": {"song": [song("1", "A"), song("2", "B")]},
    }
    api = make_api(connection)

    refs = api.get_songs_by_genre_as_refs("Rock")
    assert all(isinstance(r, Ref) and r.type == Ref.TRACK for r in refs)
    assert [r.name for r in refs] == ["A", "B"]
    assert [r.uri for r in refs] == [
        uri.get_song_uri("1"),
        uri.get_song_uri("2"),
    ]
    connection.getSongsByGenre.assert_called_once_with(
        "Rock", api.genre_songs_size, 0
    )


def test_get_songs_by_genre_filters_id_less_songs():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": {
            "song": [song("1", "A"), {"title": "no id"}, song("2", "B")]
        },
    }
    api = make_api(connection)

    refs = api.get_songs_by_genre_as_refs("Rock")
    assert [r.name for r in refs] == ["A", "B"]


def test_songs_by_genre_network_failure_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getSongsByGenre.side_effect = Exception("boom")
    api = make_api(connection)
    assert api.get_songs_by_genre_as_refs("Rock") == []


def test_songs_by_genre_non_ok_status_returns_empty():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {"status": "failed"}
    api = make_api(connection)
    assert api.get_songs_by_genre_as_refs("Rock") == []


def test_null_songs_by_genre_container_returns_empty_never_raises():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": None,
    }
    api = make_api(connection)
    assert api.get_songs_by_genre_as_refs("Rock") == []


# --- config knob -----------------------------------------------------------


def test_genre_songs_size_honoured():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": {"song": []},
    }
    api = make_api(connection)
    api.genre_songs_size = 25

    api.get_songs_by_genre_as_refs("Rock")
    connection.getSongsByGenre.assert_called_with("Rock", 25, 0)


def test_genre_songs_size_defaults_when_none():
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
            genre_songs_size=None,
        )
    assert api.genre_songs_size == subsonic_api.GENRE_SONGS_SIZE_DEFAULT


# --- Genres vdir and browse dispatch ---------------------------------------


def test_genres_appears_in_root_vdir():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri("root"))
    assert "Genres" in [r.name for r in refs]


def test_browse_genres_vdir_dispatch_lists_genres():
    connection = mock.Mock()
    connection.getGenres.return_value = {
        "status": "ok",
        "genres": {"genre": [genre("Rock"), genre("Jazz")]},
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri("genres"))
    assert [r.name for r in refs] == ["Rock", "Jazz"]
    assert all(r.type == Ref.DIRECTORY for r in refs)


def test_browse_genre_yields_song_refs():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": {"song": [song("1", "A")]},
    }
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_genre_uri("Rock"))
    assert [r.name for r in refs] == ["A"]
    assert refs[0].uri == uri.get_song_uri("1")
    assert refs[0].type == Ref.TRACK
    connection.getSongsByGenre.assert_called_once_with(
        "Rock", api.genre_songs_size, 0
    )


def test_browse_genre_special_char_name_reaches_server_verbatim():
    connection = mock.Mock()
    connection.getSongsByGenre.return_value = {
        "status": "ok",
        "songsByGenre": {"song": [song("1", "A")]},
    }
    api = make_api(connection)
    library = make_library(api)

    library.browse(uri.get_genre_uri("Drum & Bass"))
    connection.getSongsByGenre.assert_called_once_with(
        "Drum & Bass", api.genre_songs_size, 0
    )


def test_bare_genre_uri_browse_returns_empty_no_fetch():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    assert library.browse("subidy:genre:") == []
    connection.getSongsByGenre.assert_not_called()
