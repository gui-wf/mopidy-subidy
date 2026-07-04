"""Tests for search3 result shape resilience and Track/Album metadata.

Import only subsonic_api / library / uri and mock libsonic.Connection, so
they run without a Subsonic server and without pulling GStreamer. Reuse the
make_api fixture pattern from test_coverart.
"""

from unittest import mock

from mopidy.models import Artist, Track

from mopidy_subidy import subsonic_api
from mopidy_subidy.subsonic_api import (
    _int_or_none,
    _str_or_none,
    _year_to_date,
)

from tests.test_coverart import make_api, make_library


# --- pure helper coercion -------------------------------------------------


def test_int_or_none_coerces():
    assert _int_or_none(2) == 2
    assert _int_or_none("13") == 13  # stringified by an XML->dict serializer
    assert _int_or_none(None) is None
    assert _int_or_none("") is None
    assert _int_or_none("junk") is None  # broken server, no crash
    assert _int_or_none(-1) is None  # would trip Integer(min=0) in the model
    assert _int_or_none("-5") is None
    assert _int_or_none(0) == 0


def test_str_or_none_coerces():
    assert _str_or_none("mbid-x") == "mbid-x"
    assert _str_or_none("") is None
    assert _str_or_none(None) is None
    assert _str_or_none(0) is None
    # A non-str (defensive: an odd server) is coerced, never raises in intern.
    assert _str_or_none(12345) == "12345"


def test_year_to_date():
    assert _year_to_date(2009) == "2009"
    assert _year_to_date("2009") == "2009"
    assert _year_to_date(None) is None  # yearless -> omitted, NOT "None"
    assert _year_to_date(0) is None
    assert _year_to_date("junk") is None


# --- metadata mapping (the load-bearing proof) ----------------------------


def _api():
    connection = mock.Mock()
    return make_api(connection)


def test_rich_song_maps_all_metadata():
    api = _api()
    track = api.raw_song_to_track(
        {
            "id": "s1",
            "title": "Song",
            "year": 2009,
            "discNumber": 2,
            "bitRate": 160,
            "genre": "Lo-Fi",
            "musicBrainzId": "mbid-x",
            "comment": "hi",
            "track": 10,
            "duration": 328,
        }
    )
    assert isinstance(track, Track)
    assert track.date == "2009"
    assert track.disc_no == 2
    assert track.bitrate == 160
    assert track.genre == "Lo-Fi"
    assert track.musicbrainz_id == "mbid-x"
    assert track.comment == "hi"
    assert track.track_no == 10
    assert track.length == 328 * 1000


def test_yearless_song_has_no_bogus_date():
    api = _api()
    track = api.raw_song_to_track({"id": "s2", "title": "T"})
    # The old code stored the literal string "None"; assert it is truly absent.
    assert track.date is None
    assert track.musicbrainz_id is None
    assert track.comment is None


def test_broken_numeric_fields_do_not_crash():
    api = _api()
    track = api.raw_song_to_track(
        {
            "id": "s3",
            "title": "T",
            "discNumber": "junk",
            "track": -1,
            "bitRate": "not-a-number",
            "duration": "bad",
        }
    )
    assert track is not None
    assert track.disc_no is None
    assert track.track_no is None  # negative dropped, not a ValueError
    assert track.bitrate is None
    assert track.length is None


def test_stringified_numeric_fields_coerce():
    api = _api()
    track = api.raw_song_to_track(
        {"id": "s4", "title": "T", "bitRate": "192", "track": "7"}
    )
    assert track.bitrate == 192
    assert track.track_no == 7


def test_non_str_musicbrainz_id_does_not_crash():
    api = _api()
    # A defensively non-str MBID must not raise inside Identifier's sys.intern.
    track = api.raw_song_to_track(
        {"id": "s5", "title": "T", "musicBrainzId": 12345}
    )
    assert track.musicbrainz_id == "12345"


def test_rich_album_maps_metadata():
    api = _api()
    album = api.raw_album_to_album(
        {
            "id": "a1",
            "name": "Album",
            "songCount": "13",  # stringified, must coerce
            "discCount": 2,
            "year": 1999,
            "musicBrainzId": "album-mbid",
        }
    )
    assert album.num_tracks == 13
    assert album.num_discs == 2
    assert album.date == "1999"
    assert album.musicbrainz_id == "album-mbid"


def test_album_without_optional_fields():
    api = _api()
    album = api.raw_album_to_album({"id": "a2", "name": "Bare"})
    assert album.num_tracks is None
    assert album.num_discs is None
    assert album.date is None
    assert album.musicbrainz_id is None


# --- search3 single-result bare-dict shape --------------------------------


def test_find_as_search_result_single_bare_dict_artist():
    connection = mock.Mock()
    # search3 returns a single artist as a BARE dict, not a one-element list.
    connection.search3.return_value = {
        "status": "ok",
        "searchResult3": {"artist": {"id": "1", "name": "A"}},
    }
    api = make_api(connection)
    result = api.find_as_search_result("A")
    assert result is not None
    assert len(result.artists) == 1
    assert isinstance(result.artists[0], Artist)
    assert result.artists[0].name == "A"


def test_find_as_search_result_network_failure_returns_none():
    connection = mock.Mock()
    connection.search3.side_effect = Exception("network down")
    api = make_api(connection)
    assert api.find_as_search_result("A") is None


def test_find_artists_coerces_shapes():
    connection = mock.Mock()
    api = make_api(connection)

    # Bare dict -> one-element list.
    connection.search3.return_value = {
        "status": "ok",
        "searchResult3": {"artist": {"id": "1", "name": "A"}},
    }
    assert api.find_artists("A") == [{"id": "1", "name": "A"}]

    # None payload (network fail) -> [].
    connection.search3.side_effect = Exception("boom")
    assert api.find_artists("A") == []


# --- adversarial edge data: None-filtering and partial dicts ---------------


def test_non_str_genre_yields_none_no_typeerror():
    api = _api()
    # Some servers emit genre as a list (or number); Track.genre is a String
    # field that raises TypeError on a non-str. Must be dropped to None.
    track = api.raw_song_to_track(
        {"id": "s6", "title": "T", "genre": ["Rock", "Pop"]}
    )
    assert track is not None
    assert track.genre is None
    track2 = api.raw_song_to_track({"id": "s7", "title": "T", "genre": 5})
    assert track2.genre is None


def test_idless_builders_return_none():
    api = _api()
    # An id-less song/album/artist is unplayable/unbrowsable -> None.
    assert api.raw_song_to_track({"title": "no id"}) is None
    assert api.raw_album_to_album({"name": "no id"}) is None
    assert api.raw_artist_to_artist({"name": "no id"}) is None


def test_find_as_search_result_filters_idless_entries():
    connection = mock.Mock()
    # Each list mixes a valid entry with an id-less one (builder -> None).
    connection.search3.return_value = {
        "status": "ok",
        "searchResult3": {
            "artist": [{"id": "1", "name": "A"}, {"name": "no id"}],
            "album": [{"name": "no id"}, {"id": "a1", "name": "Al"}],
            "song": [{"id": "s1", "title": "S"}, {"title": "no id"}],
        },
    }
    api = make_api(connection)
    result = api.find_as_search_result("q")
    assert result is not None
    # No None leaked into any list, and the id-less entries were dropped.
    assert None not in result.artists
    assert None not in result.albums
    assert None not in result.tracks
    assert len(result.artists) == 1
    assert len(result.albums) == 1
    assert len(result.tracks) == 1
    assert result.artists[0].name == "A"


def test_search_by_artist_and_album_survives_missing_name():
    connection = mock.Mock()
    # A matched artist is fine, but one of its albums omits 'name' and another
    # omits 'id'. Neither must raise; the well-formed match still resolves.
    connection.search3.return_value = {
        "status": "ok",
        "searchResult3": {"artist": {"id": "art1", "name": "Artist"}},
    }
    connection.getArtist.return_value = {
        "status": "ok",
        "artist": {
            "album": [
                {"id": "b1"},  # no name
                {"name": "The Album"},  # no id
                {"id": "b3", "name": "The Album"},  # well-formed match
            ]
        },
    }
    connection.getAlbum.return_value = {
        "status": "ok",
        "album": {"song": [{"id": "s1", "title": "The Album track"}]},
    }
    api = make_api(connection)
    library = make_library(api)
    # Must not raise KeyError/TypeError on the partial album dicts.
    result = library.search_by_artist_and_album("Artist", "The Album")
    assert result is not None
    assert len(result.tracks) == 1
    assert result.tracks[0].name == "The Album track"


def test_search_by_artist_missing_name_and_id_does_not_crash():
    connection = mock.Mock()
    # find_artists yields a match with no id and a match with no name; neither
    # must crash the exact/non-exact filter.
    connection.search3.return_value = {
        "status": "ok",
        "searchResult3": {
            "artist": [{"name": "no id"}, {"id": "a1"}, {"id": "a2", "name": "X"}]
        },
    }
    connection.getArtist.return_value = {"status": "ok", "artist": {"album": []}}
    api = make_api(connection)
    library = make_library(api)
    result = library.search_by_artist("X", exact=False)
    assert result is not None  # no crash on the id-less / name-less entries
