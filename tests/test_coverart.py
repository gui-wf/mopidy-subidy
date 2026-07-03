"""Regression tests for cover-art resolution (get_images).

These import only subsonic_api / library / uri and mock libsonic.Connection,
so they run without a Subsonic server and without pulling GStreamer.
"""

from unittest import mock

from mopidy.models import Image

from mopidy_subidy import subsonic_api, uri
from mopidy_subidy.library import SubidyLibraryProvider

USERNAME = "alice"
PASSWORD = "s3cr3t"


def make_api(connection):
    """Build a SubsonicApi with a mocked libsonic.Connection (no network)."""
    connection.appName = "subidy"
    connection.apiVersion = "1.16.1"
    with mock.patch("libsonic.Connection", return_value=connection):
        return subsonic_api.SubsonicApi(
            url="http://example.com",
            username=USERNAME,
            password=PASSWORD,
            app_name="subidy",
            legacy_auth=False,
            api_version="1.16.1",
        )


def make_library(api):
    backend = mock.Mock()
    backend.subsonic_api = api
    return SubidyLibraryProvider(backend=backend)


def test_get_images_for_song_uses_salted_token_no_password():
    connection = mock.Mock()
    # Song was never parsed -> cold fallback hits getSong once.
    connection.getSong.return_value = {"song": {"id": "42", "coverArt": "al7"}}
    api = make_api(connection)
    library = make_library(api)

    song_uri = uri.get_song_uri("42")
    result = library.get_images([song_uri])

    images = result[song_uri]
    assert len(images) == 1
    image = images[0]
    assert isinstance(image, Image)
    url = image.uri
    assert "getCoverArt.view" in url
    assert "id=al7" in url
    # Salted-token auth, never the plaintext password.
    assert "s=" in url and "t=" in url
    assert PASSWORD not in url
    assert "p=" not in url


def test_get_images_foreign_and_none_and_empty():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    # Foreign (non-subidy) uri and an unsupported type resolve to [].
    foreign = "spotify:track:xyz"
    playlist_uri = uri.get_playlist_uri("99")
    result = library.get_images([foreign, playlist_uri])
    assert result[foreign] == []
    assert result[playlist_uri] == []

    # Empty input yields empty mapping.
    assert library.get_images([]) == {}
    # No network calls for any of the above.
    connection.getSong.assert_not_called()
    connection.getAlbum.assert_not_called()


def test_parse_time_prepopulation_avoids_network():
    connection = mock.Mock()
    api = make_api(connection)
    library = make_library(api)

    # Parsing a raw song pre-populates the cover-art cache.
    api.raw_song_to_track({"id": "42", "title": "T", "coverArt": "al7"})
    song_uri = uri.get_song_uri("42")

    result = library.get_images([song_uri])
    assert len(result[song_uri]) == 1
    # Pre-populated -> zero cold-fallback calls.
    connection.getSong.assert_not_called()


def test_cover_art_id_cache_prevents_repeated_calls():
    connection = mock.Mock()
    connection.getSong.return_value = {"song": {"id": "42", "coverArt": "al7"}}
    api = make_api(connection)

    song_uri = uri.get_song_uri("42")
    first = api.get_cover_art_id_for_uri(song_uri)
    second = api.get_cover_art_id_for_uri(song_uri)

    assert first == second == "al7"
    assert connection.getSong.call_count == 1


def test_negative_result_is_cached():
    connection = mock.Mock()
    # Artist without cover art -> resolves to None, cached.
    connection.getArtist.return_value = {"artist": {"id": "7"}}
    api = make_api(connection)

    artist_uri = uri.get_artist_uri("7")
    assert api.get_cover_art_id_for_uri(artist_uri) is None
    assert api.get_cover_art_id_for_uri(artist_uri) is None
    assert connection.getArtist.call_count == 1
    # A None cover-art id yields no images.
    library = make_library(api)
    assert library.get_images([artist_uri])[artist_uri] == []


def test_url_is_stable_across_calls():
    connection = mock.Mock()
    api = make_api(connection)
    url1 = api.get_cover_art_url("al7")
    url2 = api.get_cover_art_url("al7")
    # Same salt reused from cache -> identical URL (stable artUrl).
    assert url1 == url2
