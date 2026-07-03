"""Tests for star/love support.

These mock libsonic.Connection (reusing the make_api harness from
test_coverart) so they run with no Subsonic server and no GStreamer. They
cover the resilient wrappers, the getStarred2 helpers (including the
single-object-not-a-list coercion), and the fail-safe diff in the virtual
Starred playlist's save path.
"""

from unittest import mock

from mopidy.models import Playlist, Ref, Track

from mopidy_subidy import subsonic_api, uri
from mopidy_subidy.library import SubidyLibraryProvider
from mopidy_subidy.playlists import STARRED_URI, SubidyPlaylistsProvider

from tests.test_coverart import make_api


def make_library(api):
    backend = mock.Mock()
    backend.subsonic_api = api
    return SubidyLibraryProvider(backend=backend)


def make_playlists(api):
    backend = mock.Mock()
    backend.subsonic_api = api
    return SubidyPlaylistsProvider(backend=backend)


def starred2(songs=None, albums=None, artists=None):
    payload = {}
    if songs is not None:
        payload["song"] = songs
    if albums is not None:
        payload["album"] = albums
    if artists is not None:
        payload["artist"] = artists
    return {"status": "ok", "starred2": payload}


# --- wrappers --------------------------------------------------------------


def test_star_ok_returns_true_and_passes_song_ids():
    connection = mock.Mock()
    connection.star.return_value = {"status": "ok"}
    api = make_api(connection)

    assert api.star(song_ids=["1", "2"]) is True
    connection.star.assert_called_once_with(
        sids=["1", "2"], albumIds=[], artistIds=[]
    )


def test_star_network_failure_returns_false_never_raises():
    connection = mock.Mock()
    connection.star.side_effect = Exception("boom")
    api = make_api(connection)

    assert api.star(song_ids=["1"]) is False


def test_unstar_network_failure_returns_false():
    connection = mock.Mock()
    connection.unstar.side_effect = Exception("boom")
    api = make_api(connection)

    assert api.unstar(song_ids=["1"]) is False


def test_set_rating_out_of_range_returns_false():
    connection = mock.Mock()
    connection.setRating.side_effect = Exception("ArgumentError")
    api = make_api(connection)

    assert api.set_rating("1", 99) is False


# --- getStarred2 helpers ---------------------------------------------------


def test_get_starred_song_ids_stringifies():
    connection = mock.Mock()
    # Server returns int ids; ids must be stringified for the diff.
    connection.getStarred2.return_value = starred2(
        songs=[{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
    )
    api = make_api(connection)

    assert api.get_starred_song_ids() == {"1", "2"}


def test_get_raw_starred_failure_returns_empty_dict():
    connection = mock.Mock()
    connection.getStarred2.side_effect = Exception("boom")
    api = make_api(connection)

    assert api.get_raw_starred() == {}
    assert api.get_starred_song_ids() == set()
    assert api.get_starred_songs_as_refs() == []


def test_fetch_starred_failure_returns_none_not_empty():
    connection = mock.Mock()
    connection.getStarred2.side_effect = Exception("boom")
    api = make_api(connection)

    # None (failure) is distinct from {} so the save path can abort.
    assert api.fetch_starred() is None


def test_single_object_not_a_list_is_coerced():
    connection = mock.Mock()
    # Some servers emit a lone starred album/artist as a bare object.
    connection.getStarred2.return_value = starred2(
        albums={"id": "al1", "name": "Only Album"},
        artists={"id": "ar1", "name": "Only Artist"},
        songs={"id": "s1", "title": "Only Song"},
    )
    api = make_api(connection)

    album_refs = api.get_starred_albums_as_refs()
    artist_refs = api.get_starred_artists_as_refs()
    song_refs = api.get_starred_songs_as_refs()

    assert [r.name for r in album_refs] == ["Only Album"]
    assert [r.name for r in artist_refs] == ["Only Artist"]
    assert [r.name for r in song_refs] == ["Only Song"]
    assert api.get_starred_song_ids() == {"s1"}


def test_starred_key_fallback_from_starred2_to_starred():
    connection = mock.Mock()
    connection.getStarred2.return_value = {
        "status": "ok",
        "starred": {"song": [{"id": "s9", "title": "x"}]},
    }
    api = make_api(connection)

    assert api.get_starred_song_ids() == {"s9"}


def test_starred_cache_collapses_fanout():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "a"}],
        albums=[{"id": "al1", "name": "A"}],
        artists=[{"id": "ar1", "name": "Ar"}],
    )
    api = make_api(connection)

    # A single browse of the Starred dir reads songs+albums+artists from one
    # getStarred2 payload via the combined builder.
    refs = api.get_starred_as_refs()
    assert {r.name for r in refs} == {"a", "A", "Ar"}
    assert connection.getStarred2.call_count == 1


# --- library browse --------------------------------------------------------


def test_browse_starred_dir_lists_starred_content():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "Song"}],
        albums=[{"id": "al1", "name": "Album"}],
        artists=[{"id": "ar1", "name": "Artist"}],
    )
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri(subsonic_api.RESERVED_STARRED_ID))
    names = {r.name for r in refs}
    assert names == {"Song", "Album", "Artist"}
    # A whole browse must issue exactly one getStarred2.
    assert connection.getStarred2.call_count == 1


def test_starred_appears_in_root_browse():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    refs = library.browse(uri.get_vdir_uri("root"))
    assert "Starred" in {r.name for r in refs}


# --- virtual playlist ------------------------------------------------------


def test_as_list_prepends_virtual_starred():
    connection = mock.Mock()
    connection.getPlaylists.return_value = {
        "status": "ok",
        "playlists": {"playlist": [{"id": "7", "name": "Mix"}]},
    }
    api = make_api(connection)
    playlists = make_playlists(api)

    refs = playlists.as_list()
    assert refs[0].uri == STARRED_URI
    assert refs[0].name == "Starred"
    assert isinstance(refs[0], Ref)


def test_as_list_starred_visible_even_when_server_list_fails():
    connection = mock.Mock()
    connection.getPlaylists.side_effect = Exception("boom")
    api = make_api(connection)
    playlists = make_playlists(api)

    refs = playlists.as_list()
    assert refs[0].uri == STARRED_URI


def test_delete_virtual_starred_returns_false():
    connection = mock.Mock()
    api = make_api(connection)
    playlists = make_playlists(api)

    assert playlists.delete(STARRED_URI) is False
    connection.deletePlaylist.assert_not_called()


def test_get_items_starred_returns_track_refs():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "Song"}]
    )
    api = make_api(connection)
    playlists = make_playlists(api)

    items = playlists.get_items(STARRED_URI)
    assert all(r.type == Ref.TRACK for r in items)
    assert items[0].name == "Song"


# --- save diff -------------------------------------------------------------


def _playlist_with_song_ids(ids):
    tracks = [Track(uri=uri.get_song_uri(i)) for i in ids]
    return Playlist(uri=STARRED_URI, name="Starred", tracks=tuple(tracks))


def test_save_stars_added_and_unstars_removed():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]
    )
    connection.star.return_value = {"status": "ok"}
    connection.unstar.return_value = {"status": "ok"}
    api = make_api(connection)
    playlists = make_playlists(api)

    # Desired = {2, 3}; current = {1, 2} -> star 3, unstar 1.
    playlists.save(_playlist_with_song_ids(["2", "3"]))

    connection.star.assert_called_once_with(
        sids=["3"], albumIds=[], artistIds=[]
    )
    connection.unstar.assert_called_once_with(
        sids=["1"], albumIds=[], artistIds=[]
    )


def test_save_empty_diff_issues_no_writes():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]
    )
    api = make_api(connection)
    playlists = make_playlists(api)

    # Re-saving the identical set (ncmpcpp habitual re-save) = zero writes.
    playlists.save(_playlist_with_song_ids(["1", "2"]))

    connection.star.assert_not_called()
    connection.unstar.assert_not_called()


def test_save_empty_playlist_does_not_mass_unstar():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]
    )
    api = make_api(connection)
    playlists = make_playlists(api)

    # playlistclear Starred -> desired == {} -> must NOT unstar everything.
    playlists.save(_playlist_with_song_ids([]))

    connection.unstar.assert_not_called()
    connection.star.assert_not_called()


def test_save_aborts_when_current_read_fails():
    connection = mock.Mock()
    # The read of the current starred set fails -> abort, never unstar.
    connection.getStarred2.side_effect = Exception("boom")
    api = make_api(connection)
    playlists = make_playlists(api)

    playlists.save(_playlist_with_song_ids(["3"]))

    connection.star.assert_not_called()
    connection.unstar.assert_not_called()


def test_save_ignores_non_song_tracks():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(songs=[])
    connection.star.return_value = {"status": "ok"}
    api = make_api(connection)
    playlists = make_playlists(api)

    tracks = (
        Track(uri=uri.get_song_uri("5")),
        Track(uri="spotify:track:foreign"),
        Track(uri=uri.get_album_uri("al1")),
        Track(uri=None),
    )
    playlists.save(Playlist(uri=STARRED_URI, name="Starred", tracks=tracks))

    # Only the real subidy song is starred; foreign/album/None uris are
    # ignored (and a None uri does not raise).
    connection.star.assert_called_once_with(
        sids=["5"], albumIds=[], artistIds=[]
    )


def test_save_reads_starred_only_once():
    connection = mock.Mock()
    connection.getStarred2.return_value = starred2(
        songs=[{"id": "1", "title": "a"}]
    )
    api = make_api(connection)
    playlists = make_playlists(api)

    # No-op save (desired == current): must not fire a second getStarred2 to
    # build the returned view.
    playlists.save(_playlist_with_song_ids(["1"]))

    assert connection.getStarred2.call_count == 1
    connection.star.assert_not_called()
    connection.unstar.assert_not_called()


def test_save_none_current_read_skips_all_destructive_ops():
    connection = mock.Mock()
    # Current read is unknown (failure) -> no star, no unstar, ever.
    connection.getStarred2.side_effect = Exception("boom")
    api = make_api(connection)
    playlists = make_playlists(api)

    result = playlists.save(_playlist_with_song_ids(["1", "2"]))

    connection.star.assert_not_called()
    connection.unstar.assert_not_called()
    # Returns a view (not a crash); tracks unknown -> empty.
    assert result.uri == STARRED_URI
