"""Tests for the short-TTL bounded LRU listing cache (performance-caching).

Two layers are covered with no Subsonic server and no GStreamer:

  1. The _TtlLru primitive directly (miss/hit, LRU eviction, TTL expiry,
     cached-empty-is-a-hit, ttl<=0 off switch), with time.monotonic
     monkeypatched so no test sleeps.
  2. The cache-through behavior of the wrapped fetchers against a mocked
     libsonic.Connection: that a repeat browse collapses the round-trip, that
     the TTL re-fetches, that `random` and getStarred2 are NOT collapsed, that
     star() does not bust the listing cache, and - critically for the
     resilience bar - that a transient failure (None / []) is NOT cached
     (no sticky-empty dir, no sticky crash).
"""

from unittest import mock

from mopidy_subidy import subsonic_api
from mopidy_subidy.subsonic_api import _TtlLru

from tests.test_coverart import make_api


# --- _TtlLru primitive -----------------------------------------------------


def test_ttllru_miss_then_hit():
    cache = _TtlLru(maxsize=8, ttl=30)
    assert cache.get("k") == (False, None)
    cache.set("k", ["v"])
    assert cache.get("k") == (True, ["v"])


def test_ttllru_cached_empty_is_a_hit_not_a_miss():
    # A legitimately-cached empty value must be distinguishable from a miss so
    # a truly-empty listing is not re-fetched every browse within the TTL.
    cache = _TtlLru(maxsize=8, ttl=30)
    cache.set("k", [])
    assert cache.get("k") == (True, [])
    cache.set("n", None)
    assert cache.get("n") == (True, None)


def test_ttllru_evicts_lru_at_maxsize():
    cache = _TtlLru(maxsize=2, ttl=30)
    cache.set("a", 1)
    cache.set("b", 2)
    # Touch "a" so "b" becomes the least-recently-used.
    assert cache.get("a") == (True, 1)
    cache.set("c", 3)
    assert cache.get("b") == (False, None)  # evicted
    assert cache.get("a") == (True, 1)
    assert cache.get("c") == (True, 3)


def test_ttllru_expiry_drops_the_key(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        subsonic_api.time, "monotonic", lambda: clock["t"]
    )
    cache = _TtlLru(maxsize=8, ttl=30)
    cache.set("k", ["v"])
    assert cache.get("k") == (True, ["v"])
    clock["t"] += 30  # exactly ttl -> expired (>= ttl)
    assert cache.get("k") == (False, None)
    # The expired key was dropped, not left dangling.
    assert cache._d == {}


def test_ttllru_ttl_zero_is_off_switch():
    cache = _TtlLru(maxsize=8, ttl=0)
    cache.set("k", ["v"])
    assert cache.get("k") == (False, None)
    assert cache._d == {}


# --- cache-through: collapse + TTL re-fetch --------------------------------


def _artists_payload(names):
    return {
        "status": "ok",
        "artists": {
            "index": [
                {"artist": [{"id": n, "name": n} for n in names]}
            ]
        },
    }


def test_get_raw_artists_collapses_repeat_roundtrip(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        subsonic_api.time, "monotonic", lambda: clock["t"]
    )
    connection = mock.Mock()
    connection.getArtists.return_value = _artists_payload(["A", "B"])
    api = make_api(connection)

    first = api.get_raw_artists()
    second = api.get_raw_artists()
    assert first == second
    assert connection.getArtists.call_count == 1  # collapsed

    # Past the TTL, the next browse re-fetches.
    clock["t"] += subsonic_api.LISTING_CACHE_TTL_DEFAULT
    api.get_raw_artists()
    assert connection.getArtists.call_count == 2


def test_listing_cache_ttl_zero_disables_collapse():
    connection = mock.Mock()
    connection.getArtists.return_value = _artists_payload(["A"])
    with mock.patch("libsonic.Connection", return_value=connection):
        connection.appName = "subidy"
        connection.apiVersion = "1.16.1"
        api = subsonic_api.SubsonicApi(
            url="http://example.com",
            username="alice",
            password="s3cr3t",
            app_name="subidy",
            legacy_auth=False,
            api_version="1.16.1",
            listing_cache_ttl=0,
        )
    api.get_raw_artists()
    api.get_raw_artists()
    assert connection.getArtists.call_count == 2  # cache off


# --- resilience: failure/empty results are NOT cached ----------------------


def test_transient_dir_failure_is_not_cached():
    # A failed getMusicDirectory returns None; caching it would freeze the dir
    # empty (and re-trigger the get_diritems None-guard) for a full TTL. It
    # must self-heal on the next browse.
    connection = mock.Mock()
    connection.getMusicDirectory.side_effect = [
        Exception("boom"),
        {"status": "ok", "directory": {"child": [
            {"isDir": False, "id": "s1", "title": "Song", "track": 1}
        ]}},
    ]
    api = make_api(connection)

    assert api.get_raw_dir("d1") is None  # failure, not cached
    diritems = api.get_raw_dir("d1")  # retried
    assert diritems and diritems[0]["id"] == "s1"
    assert connection.getMusicDirectory.call_count == 2


def test_get_diritems_as_refs_survives_none_dir():
    # The pre-existing None-iteration crash is guarded: a failed dir browse is
    # an empty listing, never a TypeError.
    connection = mock.Mock()
    connection.getMusicDirectory.side_effect = Exception("boom")
    api = make_api(connection)
    assert api.get_diritems_as_refs("d1") == []


def test_transient_artists_failure_is_not_cached():
    connection = mock.Mock()
    connection.getArtists.side_effect = [
        Exception("boom"),
        _artists_payload(["A"]),
    ]
    api = make_api(connection)
    # Failure is the None sentinel (distinct from an empty-but-real []); it is
    # NOT cached, so the next browse re-fetches and self-heals.
    assert api.get_raw_artists() is None
    assert [a["name"] for a in api.get_raw_artists()] == ["A"]  # retried
    assert connection.getArtists.call_count == 2


# --- empty-but-successful listings ARE cached (static empty data) ----------


def _empty_artists_payload():
    # status ok, an index present but with no artists -> empty-but-real library.
    return {"status": "ok", "artists": {"index": []}}


def test_empty_but_successful_artists_is_cached():
    # A genuinely empty (but successful) getArtists yields [] and MUST be
    # cached, so an empty library is not re-fetched on every browse.
    connection = mock.Mock()
    connection.getArtists.return_value = _empty_artists_payload()
    api = make_api(connection)

    assert api.get_raw_artists() == []
    assert api.get_raw_artists() == []  # served from cache
    assert connection.getArtists.call_count == 1  # collapsed, [] is a hit


def test_failure_returns_empty_list_to_caller_and_is_not_cached():
    # The None failure sentinel degrades to [] for the ref-building caller
    # (never a crash), and is not cached so the next browse self-heals.
    connection = mock.Mock()
    connection.getArtists.side_effect = [
        Exception("boom"),
        _artists_payload(["A"]),
    ]
    api = make_api(connection)
    assert api.get_artists_as_refs() == []  # failure -> [] to caller
    refs = api.get_artists_as_refs()  # retried, not stuck-empty
    assert [r.name for r in refs] == ["A"]
    assert connection.getArtists.call_count == 2


# --- empty directory: browse/queue an empty dir returns [] cleanly ---------


def test_empty_directory_child_absent_yields_empty_refs():
    # getMusicDirectory success with no 'child' key is an EMPTY directory, not
    # a failure. It must yield [] refs (not raise a TypeError on sorted(None)).
    connection = mock.Mock()
    connection.getMusicDirectory.return_value = {
        "status": "ok",
        "directory": {"id": "d1"},  # no 'child' key at all
    }
    api = make_api(connection)
    assert api.get_raw_dir("d1") == []
    assert api.get_diritems_as_refs("d1") == []
    # Empty-but-successful dir is cacheable: second browse is collapsed.
    api.get_raw_dir("d1")
    assert connection.getMusicDirectory.call_count == 1


def test_empty_directory_child_none_yields_empty_refs():
    # Some servers emit 'child': None for an empty dir; same clean [] contract.
    connection = mock.Mock()
    connection.getMusicDirectory.return_value = {
        "status": "ok",
        "directory": {"id": "d1", "child": None},
    }
    api = make_api(connection)
    assert api.get_raw_dir("d1") == []
    assert api.get_diritems_as_refs("d1") == []


# --- album smart-lists: random bypass, volatile lists cached ---------------


def _albumlist2(ids):
    return {
        "status": "ok",
        "albumList2": {"album": [{"id": i, "name": i} for i in ids]},
    }


def test_random_smart_list_is_not_cached():
    connection = mock.Mock()
    connection.getAlbumList2.return_value = _albumlist2(["al1"])
    api = make_api(connection)

    api.get_album_list_as_refs("random")
    api.get_album_list_as_refs("random")
    assert connection.getAlbumList2.call_count == 2  # fresh per browse


def test_volatile_smart_list_is_cached_within_ttl(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        subsonic_api.time, "monotonic", lambda: clock["t"]
    )
    connection = mock.Mock()
    connection.getAlbumList2.return_value = _albumlist2(["al1"])
    api = make_api(connection)

    # frequent is play-driven (volatile) but cached as a browse-fan-out
    # collapse within the short TTL.
    api.get_album_list_as_refs("frequent")
    api.get_album_list_as_refs("frequent")
    assert connection.getAlbumList2.call_count == 1
    clock["t"] += subsonic_api.LISTING_CACHE_TTL_DEFAULT
    api.get_album_list_as_refs("frequent")
    assert connection.getAlbumList2.call_count == 2


def test_full_library_album_loop_is_not_listing_cached():
    # get_raw_album_list (the size=500 alphabeticalByName loop for the Albums
    # vdir) goes through get_more_albums, which is NOT wrapped, so it never
    # collides with the smart-list cache keys. Two full walks both hit network.
    connection = mock.Mock()
    connection.getAlbumList2.return_value = {
        "status": "ok",
        "albumList2": {"album": []},
    }
    api = make_api(connection)
    api.get_raw_album_list("alphabeticalByName")
    api.get_raw_album_list("alphabeticalByName")
    assert connection.getAlbumList2.call_count == 2


# --- invalidation independence: star does not bust the listing cache -------


def test_star_does_not_bust_listing_cache():
    connection = mock.Mock()
    connection.getArtists.return_value = _artists_payload(["A"])
    connection.star.return_value = {"status": "ok"}
    api = make_api(connection)

    api.get_raw_artists()
    assert api.star(song_ids=["1"]) is True
    api.get_raw_artists()
    # star busts only the starred cache; the artist listing stays cached.
    assert connection.getArtists.call_count == 1


def test_starred_is_not_listing_cached_and_stays_live_after_star():
    connection = mock.Mock()
    connection.getStarred2.return_value = {
        "status": "ok",
        "starred2": {"song": [{"id": "1", "title": "a"}]},
    }
    connection.star.return_value = {"status": "ok"}
    api = make_api(connection)

    api.get_starred_song_ids()
    api.star(song_ids=["2"])  # busts _starred_cache
    api.get_starred_song_ids()
    # star invalidated the starred cache -> a fresh getStarred2 fires.
    assert connection.getStarred2.call_count == 2
