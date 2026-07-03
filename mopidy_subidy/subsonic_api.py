import logging
import os
import re
from collections import OrderedDict
from hashlib import md5
from urllib.parse import urlencode, urlparse

import libsonic
from mopidy.models import Album, Artist, Playlist, Ref, SearchResult, Track
from mopidy_subidy import uri

logger = logging.getLogger(__name__)

RESPONSE_OK = "ok"
UNKNOWN_SONG = "Unknown Song"
UNKNOWN_ALBUM = "Unknown Album"
UNKNOWN_ARTIST = "Unknown Artist"
MAX_SEARCH_RESULTS = 100
MAX_LIST_RESULTS = 500

# Stable cover-art size (px) requested from getCoverArt. Kept as a plain
# constant rather than a config key to keep the extension's footprint small;
# the value is baked into the image URL so a given track always yields the
# same URL (which lets GNOME/MPRIS cache art by URL).
DEFAULT_IMAGE_SIZE = 600

# Upper bound on the two cover-art caches so a long-lived service does not
# grow them without limit. Both are LRU-evicted (oldest first) at the cap, so
# a URL that was handed out stays stable as long as it is still in use.
COVER_ART_CACHE_MAX = 4096


def ref_sort_key(ref):
    return ref.name


def string_nums_nocase_sort_key(s):
    segments = []
    for substr in re.split(r"(\d+)", s):
        if substr.isdigit():
            seg = int(substr)
        else:
            seg = substr.lower()
        segments.append(seg)
    return segments


def diritem_sort_key(item):
    isdir = item["isDir"]
    if isdir:
        key = string_nums_nocase_sort_key(item["title"])
    else:
        key = int(item.get("track", 1))
    return (isdir, key)


class SubsonicApi:
    def __init__(
        self, url, username, password, app_name, legacy_auth, api_version
    ):
        parsed = urlparse(url)
        self.port = (
            parsed.port
            if parsed.port
            else 443
            if parsed.scheme == "https"
            else 80
        )
        base_url = parsed.scheme + "://" + parsed.hostname
        self.connection = libsonic.Connection(
            base_url,
            username,
            password,
            self.port,
            parsed.path + "/rest",
            appName=app_name,
            legacyAuth=legacy_auth,
            apiVersion=api_version,
        )
        self.url = url + "/rest"
        self.username = username
        self.password = password
        # Cover-art caches. Both are mutated and read ONLY on the backend
        # actor thread (SubidyBackend is a pykka.ThreadingActor, and
        # core.library.get_images is routed through that actor), so a plain
        # dict is safe here without locking.
        #   _cover_art_id_cache: mopidy uri -> Subsonic coverArt id (or None).
        #     Pre-populated at parse time (raw_song_to_track / raw_album_to_album)
        #     so the common case (a browsed or playing item) resolves with zero
        #     network calls. Negatives are cached so artless URIs are not
        #     re-resolved. LRU-evicted at COVER_ART_CACHE_MAX.
        #   _cover_art_url_cache: (id, size) -> fully built getCoverArt URL.
        #     Required because a fresh salt is drawn on every build, so building
        #     the URL twice yields two different strings; caching keeps a
        #     track's artUrl stable. LRU-evicted at COVER_ART_CACHE_MAX.
        self._cover_art_id_cache = OrderedDict()
        self._cover_art_url_cache = OrderedDict()
        logger.info(
            f"Connecting to subsonic server on url {url} as user {username}, "
            f"API version {api_version}"
        )
        try:
            self.connection.ping()
        except Exception as e:
            logger.error("Unable to reach subsonic server: %s" % e)
            exit()

    def get_subsonic_uri(self, view_name, params, censor=False):
        di_params = {}
        di_params.update(params)
        di_params.update(c=self.connection.appName)
        di_params.update(v=self.connection.apiVersion)
        if censor:
            di_params.update(u="*****", p="*****")
        else:
            di_params.update(u=self.username, p=self.password)
        return "{}/{}.view?{}".format(self.url, view_name, urlencode(di_params))

    def get_song_stream_uri(self, song_id):
        return self.get_subsonic_uri("stream", dict(id=song_id))

    def get_censored_song_stream_uri(self, song_id):
        return self.get_subsonic_uri("stream", dict(id=song_id), True)

    def _cover_art_auth_qdict(self):
        """Build the auth query params for a cover-art URL.

        Always uses salt+token, never the plaintext password and never the
        legacy ``p=enc:<hex>`` form (which is a reversible hex of the real
        password). This deliberately does NOT reuse the plaintext stream path
        (get_subsonic_uri) nor py-sonic's _getBaseQdict, so the resulting
        image URL is safe to hand to GNOME/MPRIS even under legacy_auth.
        """
        salt = md5(os.urandom(100)).hexdigest()[:12]
        token = md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "s": salt,
            "t": token,
            "v": self.connection.apiVersion,
            "c": self.connection.appName,
        }

    def get_cover_art_url(self, cover_art_id, size=None):
        """Return a directly-fetchable getCoverArt URL, or None.

        The built URL is cached per (cover_art_id, size) for the process
        lifetime so a given item always yields the same URL (salt is fixed at
        first build). The URL is never logged: it carries a salted token.
        """
        if not cover_art_id:
            return None
        if size is None:
            size = DEFAULT_IMAGE_SIZE
        cover_art_id = str(cover_art_id)
        cache_key = (cover_art_id, size)
        cached = self._cover_art_url_cache.get(cache_key)
        if cached is not None:
            self._cover_art_url_cache.move_to_end(cache_key)
            return cached
        qdict = self._cover_art_auth_qdict()
        qdict.update(id=cover_art_id, size=size)
        url = "{}/getCoverArt.view?{}".format(self.url, urlencode(qdict))
        self._cover_art_url_cache[cache_key] = url
        while len(self._cover_art_url_cache) > COVER_ART_CACHE_MAX:
            self._cover_art_url_cache.popitem(last=False)
        return url

    def _remember_cover_art_id(self, mopidy_uri, cover_art_id):
        """Record a uri -> coverArt id mapping (LRU, negatives allowed)."""
        if cover_art_id is not None:
            cover_art_id = str(cover_art_id)
        self._cover_art_id_cache[mopidy_uri] = cover_art_id
        self._cover_art_id_cache.move_to_end(mopidy_uri)
        while len(self._cover_art_id_cache) > COVER_ART_CACHE_MAX:
            self._cover_art_id_cache.popitem(last=False)
        return cover_art_id

    def get_cover_art_id_for_uri(self, mopidy_uri):
        """Resolve a subidy URI to a Subsonic coverArt id, cache-first.

        The cache is pre-populated at parse time for every song/album that was
        browsed or is playing, so the normal path returns here with zero
        network calls. The cold fallback below (a getSong/getAlbum/getArtist
        call) is hit only for a uri that was never parsed - essentially a cold
        MPRIS query for a single uri, which may block briefly. Negatives are
        cached too, so artless URIs are not re-fetched on every metadata poll.
        Never raises.
        """
        if mopidy_uri in self._cover_art_id_cache:
            self._cover_art_id_cache.move_to_end(mopidy_uri)
            return self._cover_art_id_cache[mopidy_uri]
        uri_type = uri.get_type(mopidy_uri)
        cover_art_id = None
        try:
            if uri_type == uri.SONG:
                song_id = uri.get_song_id(mopidy_uri)
                song = self.connection.getSong(song_id).get("song") or {}
                cover_art_id = song.get("coverArt") or song_id
            elif uri_type == uri.ALBUM:
                album_id = uri.get_album_id(mopidy_uri)
                album = self.connection.getAlbum(album_id).get("album") or {}
                cover_art_id = album.get("coverArt") or album_id
            elif uri_type == uri.ARTIST:
                artist_id = uri.get_artist_id(mopidy_uri)
                artist = (
                    self.connection.getArtist(artist_id).get("artist") or {}
                )
                cover_art_id = artist.get("coverArt")
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when resolving cover art."
            )
        return self._remember_cover_art_id(mopidy_uri, cover_art_id)

    def find_raw(
        self,
        query,
        exclude_artists=False,
        exclude_albums=False,
        exclude_songs=False,
    ):
        try:
            response = self.connection.search3(
                query.encode("utf-8"),
                MAX_SEARCH_RESULTS if not exclude_artists else 0,
                0,
                MAX_SEARCH_RESULTS if not exclude_albums else 0,
                0,
                MAX_SEARCH_RESULTS if not exclude_songs else 0,
                0,
            )
        except Exception:
            logger.warning("Connecting to subsonic failed when searching.")
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response.get("searchResult3")

    def find_as_search_result(
        self,
        query,
        exclude_artists=False,
        exclude_albums=False,
        exclude_songs=False,
    ):
        result = self.find_raw(query)
        if result is None:
            return None
        return SearchResult(
            uri=uri.get_search_uri(query),
            artists=[
                self.raw_artist_to_artist(artist)
                for artist in result.get("artist") or []
            ],
            albums=[
                self.raw_album_to_album(album)
                for album in result.get("album") or []
            ],
            tracks=[
                self.raw_song_to_track(song)
                for song in result.get("song") or []
            ],
        )

    def create_playlist_raw(self, name):
        try:
            response = self.connection.createPlaylist(name=name)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when creating playlist."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response

    def delete_playlist_raw(self, playlist_id):
        try:
            response = self.connection.deletePlaylist(playlist_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when deleting playlist."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response

    def save_playlist_raw(self, playlist_id, song_ids):
        try:
            response = self.connection.createPlaylist(
                playlist_id, songIds=song_ids
            )
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when creating playlist."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response

    def get_raw_artists(self):
        try:
            response = self.connection.getArtists()
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading list of artists."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        letters = response.get("artists").get("index")
        if letters is not None:
            artists = [
                artist
                for letter in letters
                for artist in letter.get("artist") or []
            ]
            return artists
        logger.warning(
            "Subsonic does not seem to have any artists in it's library."
        )
        return []

    def get_raw_rootdirs(self):
        try:
            response = self.connection.getIndexes()
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading list of rootdirs."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        letters = response.get("indexes").get("index")
        if letters is not None:
            artists = [
                artist
                for letter in letters
                for artist in letter.get("artist") or []
            ]
            return artists
        logger.warning(
            "Subsonic does not seem to have any rootdirs in its library."
        )
        return []

    def get_song_by_id(self, song_id):
        try:
            response = self.connection.getSong(song_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading song by id."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return (
            self.raw_song_to_track(response.get("song"))
            if response.get("song") is not None
            else None
        )

    def scrobble(self, song_id, submission=True):
        try:
            response = self.connection.scrobble(song_id, submission=submission)
        except Exception:
            logger.warning("Connecting to subsonic failed when scrobbling.")
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response

    def get_album_by_id(self, album_id):
        try:
            response = self.connection.getAlbum(album_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading album by id."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return (
            self.raw_album_to_album(response.get("album"))
            if response.get("album") is not None
            else None
        )

    def get_artist_by_id(self, artist_id):
        try:
            response = self.connection.getArtist(artist_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading artist by id."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return (
            self.raw_artist_to_artist(response.get("artist"))
            if response.get("artist") is not None
            else None
        )

    def get_raw_playlists(self):
        try:
            response = self.connection.getPlaylists()
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading list of playlists."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        playlists = response.get("playlists").get("playlist")
        if playlists is None:
            logger.warning(
                "Subsonic does not seem to have any playlists in it's library."
            )
            return []
        return playlists

    def get_raw_playlist(self, playlist_id):
        try:
            response = self.connection.getPlaylist(playlist_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading playlist."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        return response.get("playlist")

    def get_raw_dir(self, parent_id):
        try:
            response = self.connection.getMusicDirectory(parent_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when listing content of music directory."
            )
            return None
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return None
        directory = response.get("directory")
        if directory is not None:
            diritems = directory.get("child")
            return sorted(diritems, key=diritem_sort_key)
        return None

    def get_raw_albums(self, artist_id):
        try:
            response = self.connection.getArtist(artist_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading list of albums."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        albums = response.get("artist").get("album")
        if albums is not None:
            return sorted(
                albums,
                key=lambda album: string_nums_nocase_sort_key(album["name"]),
            )
        return []

    def get_raw_songs(self, album_id):
        try:
            response = self.connection.getAlbum(album_id)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading list of songs in album."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        songs = response.get("album").get("song")
        if songs is not None:
            return songs
        return []

    def get_raw_random_song(self, size=MAX_LIST_RESULTS):
        try:
            response = self.connection.getRandomSongs(size)
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading ramdom song list."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        songs = response.get("randomSongs").get("song")
        if songs is not None:
            return songs
        return []

    def get_more_albums(self, ltype, size=MAX_LIST_RESULTS, offset=0):
        try:
            response = self.connection.getAlbumList2(
                ltype=ltype, size=size, offset=offset
            )
        except Exception:
            logger.warning(
                "Connecting to subsonic failed when loading album list."
            )
            return []
        if response.get("status") != RESPONSE_OK:
            logger.warning(
                "Got non-okay status code from subsonic: %s"
                % response.get("status")
            )
            return []
        albums = response.get("albumList2").get("album")
        if albums is not None:
            return albums
        return []

    def get_raw_album_list(self, ltype, size=MAX_LIST_RESULTS):
        """
        Subsonic servers don't offer any way to retrieve the total number
        of albums to get, and the spec states that the max number returned
        for `getAlbumList2` is 500.  To get all the albums, we make a
        `getAlbumList2` request each time the response contains 500 albums. If
        it does not, we assume we have all the albums and return them.
        """
        offset = 0
        total = []
        albums = self.get_more_albums(ltype, size, offset)
        total = albums
        while len(albums) == size:
            offset = offset + size
            albums = self.get_more_albums(ltype, size, offset)
            total = total + albums
        return total

    def get_albums_as_refs(self, artist_id=None):
        albums = (
            self.get_raw_album_list("alphabeticalByName")
            if artist_id is None
            else self.get_raw_albums(artist_id)
        )
        return [self.raw_album_to_ref(album) for album in albums]

    def get_albums_as_albums(self, artist_id):
        return [
            self.raw_album_to_album(album)
            for album in self.get_raw_albums(artist_id)
        ]

    def get_songs_as_refs(self, album_id):
        return [
            self.raw_song_to_ref(song) for song in self.get_raw_songs(album_id)
        ]

    def get_songs_as_tracks(self, album_id):
        return [
            self.raw_song_to_track(song)
            for song in self.get_raw_songs(album_id)
        ]

    def get_artists_as_refs(self):
        return [
            self.raw_artist_to_ref(artist) for artist in self.get_raw_artists()
        ]

    def get_rootdirs_as_refs(self):
        return [
            self.raw_directory_to_ref(rootdir)
            for rootdir in self.get_raw_rootdirs()
        ]

    def get_diritems_as_refs(self, directory_id):
        return [
            (
                self.raw_directory_to_ref(diritem)
                if diritem.get("isDir")
                else self.raw_song_to_ref(diritem)
            )
            for diritem in self.get_raw_dir(directory_id)
        ]

    def get_random_songs_as_refs(self):
        return [
            self.raw_song_to_ref(song) for song in self.get_raw_random_song(75)
        ]

    def get_random_songs_as_tracks(self):
        return [
            self.raw_song_to_track(song) for song in self.get_raw_random_song()
        ]

    def get_artists_as_artists(self):
        return [
            self.raw_artist_to_artist(artist)
            for artist in self.get_raw_artists()
        ]

    def get_playlists_as_refs(self):
        return [
            self.raw_playlist_to_ref(playlist)
            for playlist in self.get_raw_playlists()
        ]

    def get_playlists_as_playlists(self):
        return [
            self.raw_playlist_to_playlist(playlist)
            for playlist in self.get_raw_playlists()
        ]

    def get_playlist_as_playlist(self, playlist_id):
        return self.raw_playlist_to_playlist(self.get_raw_playlist(playlist_id))

    def get_playlist_as_songs_as_refs(self, playlist_id):
        playlist = self.get_raw_playlist(playlist_id)
        if playlist is None:
            return None
        return [self.raw_song_to_ref(song) for song in playlist.get("entry")]

    def get_artist_as_songs_as_tracks_iter(self, artist_id):
        albums = self.get_raw_albums(artist_id)
        if albums is None:
            return
        for album in albums:
            for song in self.get_raw_songs(album.get("id")):
                yield self.raw_song_to_track(song)

    def get_recursive_dir_as_songs_as_tracks_iter(self, directory_id):
        diritems = self.get_raw_dir(directory_id)
        if diritems is None:
            return
        for item in diritems:
            if item.get("isDir"):
                yield from self.get_recursive_dir_as_songs_as_tracks_iter(
                    item.get("id")
                )
            else:
                yield self.raw_song_to_track(item)

    def raw_song_to_ref(self, song):
        if song is None:
            return None
        return Ref.track(
            name=song.get("title") or UNKNOWN_SONG,
            uri=uri.get_song_uri(song.get("id")),
        )

    def raw_song_to_track(self, song):
        if song is None:
            return None
        song_uri = uri.get_song_uri(song.get("id"))
        # Pre-populate the cover-art cache so get_images resolves this track
        # with no extra network call. Fall back to the song id (a valid
        # getCoverArt id on every Subsonic server) when no coverArt is given.
        self._remember_cover_art_id(
            song_uri, song.get("coverArt") or song.get("id")
        )
        return Track(
            name=song.get("title") or UNKNOWN_SONG,
            uri=song_uri,
            bitrate=song.get("bitRate"),
            track_no=int(song.get("track")) if song.get("track") else None,
            date=str(song.get("year")) or "none",
            genre=song.get("genre"),
            length=int(song.get("duration")) * 1000
            if song.get("duration")
            else None,
            disc_no=int(song.get("discNumber"))
            if song.get("discNumber")
            else None,
            artists=[
                Artist(
                    name=song.get("artist"),
                    uri=uri.get_artist_uri(song.get("artistId")),
                )
            ],
            album=Album(
                name=song.get("album"),
                uri=uri.get_album_uri(song.get("albumId")),
            ),
        )

    def raw_album_to_ref(self, album):
        if album is None:
            return None
        return Ref.album(
            name=album.get("title") or album.get("name") or UNKNOWN_ALBUM,
            uri=uri.get_album_uri(album.get("id")),
        )

    def raw_album_to_album(self, album):
        if album is None:
            return None
        album_uri = uri.get_album_uri(album.get("id"))
        # Pre-populate the cover-art cache (see raw_song_to_track).
        self._remember_cover_art_id(
            album_uri, album.get("coverArt") or album.get("id")
        )
        return Album(
            name=album.get("title") or album.get("name") or UNKNOWN_ALBUM,
            num_tracks=album.get("songCount"),
            uri=album_uri,
            artists=[
                Artist(
                    name=album.get("artist"),
                    uri=uri.get_artist_uri(album.get("artistId")),
                )
            ],
        )

    def raw_directory_to_ref(self, directory):
        if directory is None:
            return None
        return Ref.directory(
            name=directory.get("title") or directory.get("name"),
            uri=uri.get_directory_uri(directory.get("id")),
        )

    def raw_artist_to_ref(self, artist):
        if artist is None:
            return None
        return Ref.artist(
            name=artist.get("name") or UNKNOWN_ARTIST,
            uri=uri.get_artist_uri(artist.get("id")),
        )

    def raw_artist_to_artist(self, artist):
        if artist is None:
            return None
        return Artist(
            name=artist.get("name") or UNKNOWN_ARTIST,
            uri=uri.get_artist_uri(artist.get("id")),
        )

    def raw_playlist_to_playlist(self, playlist):
        if playlist is None:
            return None
        entries = playlist.get("entry")
        tracks = (
            [self.raw_song_to_track(song) for song in entries]
            if entries is not None
            else None
        )
        return Playlist(
            uri=uri.get_playlist_uri(playlist.get("id")),
            name=playlist.get("name"),
            tracks=tracks,
        )

    def raw_playlist_to_ref(self, playlist):
        if playlist is None:
            return None
        return Ref.playlist(
            uri=uri.get_playlist_uri(playlist.get("id")),
            name=playlist.get("name"),
        )
