import logging

from mopidy import backend
from mopidy.models import Image, Ref, SearchResult
from mopidy_subidy import subsonic_api, uri

logger = logging.getLogger(__name__)

# The album smart-lists surfaced under the "Lists" vdir, in a fixed display
# order. Each pair is (human name, getAlbumList2 ltype token). Starred is
# intentionally absent - it already has its own top-level vdir and is
# referenced, not duplicated. Note: `frequent`/`recent`/`highest` depend on the
# server tracking play counts / ratings (Navidrome does); a server that does
# not will simply return an empty dir here, never an error. Single source of
# truth - _VALID_LIST_TYPES is derived from it so the two cannot drift.
_ALBUM_LISTS = [
    ("Most Played", "frequent"),
    ("Recently Added", "newest"),
    ("Recently Played", "recent"),
    ("Highest Rated", "highest"),
    ("Random", "random"),
]
_VALID_LIST_TYPES = frozenset(ltype for _, ltype in _ALBUM_LISTS)


class SubidyLibraryProvider(backend.LibraryProvider):
    def __create_vdirs():
        vdir_templates = [
            dict(id="root", name="Subsonic"),
            dict(id="artists", name="Artists"),
            dict(id="albums", name="Albums"),
            dict(id="rootdirs", name="Directories"),
            dict(id="radio", name="Radio"),
            dict(id="lists", name="Lists"),
            dict(id="genres", name="Genres"),
            dict(id="random", name="Random Songs"),
            dict(id=subsonic_api.RESERVED_STARRED_ID, name="Starred"),
        ]
        # Create a dict with the keys being the `id`s in `vdir_templates`
        # and the values being objects containing the vdir `id`,
        # the human readable name as `name`, and the URI as `uri`.
        vdirs = {}
        for template in vdir_templates:
            vdir = template.copy()
            vdir.update(uri=uri.get_vdir_uri(vdir["id"]))
            vdirs[template["id"]] = vdir
        return vdirs

    _vdirs = __create_vdirs()

    def __raw_vdir_to_ref(vdir):
        if vdir is None:
            return None
        return Ref.directory(name=vdir["name"], uri=vdir["uri"])

    root_directory = __raw_vdir_to_ref(_vdirs["root"])

    _raw_vdir_to_ref = staticmethod(__raw_vdir_to_ref)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subsonic_api = self.backend.subsonic_api

    def browse_songs(self, album_id):
        return self.subsonic_api.get_songs_as_refs(album_id)

    def browse_albums(self, artist_id=None):
        return self.subsonic_api.get_albums_as_refs(artist_id)

    def browse_artists(self):
        return self.subsonic_api.get_artists_as_refs()

    def browse_rootdirs(self):
        return self.subsonic_api.get_rootdirs_as_refs()

    def browse_random_songs(self):
        return self.subsonic_api.get_random_songs_as_refs()

    def browse_genres(self):
        """List all genres under the top-level Genres dir.

        One directory ref per genre (getGenres), each a subidy:genre:<name> uri
        that browses into that genre's songs on demand. Resilient: the helper
        logs and returns [] on any network/server failure, so the dir is simply
        empty - never an error.
        """
        return self.subsonic_api.get_genres_as_refs()

    def browse_radio(self):
        """List the algorithmic-radio surfaces under the top-level Radio dir.

        Currently a single 'Random Songs' child backed by getRandomSongs; it
        is the discoverable home for the global mix surfaces. The child is a
        directory ref browsed on demand (stays random per browse - nothing is
        cached).
        """
        return [self._raw_vdir_to_ref(self._vdirs["random"])]

    def browse_lists(self):
        """List the album smart-lists under the top-level Lists dir.

        A fixed, deterministically-ordered set of directory refs (see
        _ALBUM_LISTS), each a subidy:list:<ltype> uri that browses into a
        capped page of album refs on demand. Nothing is cached, so the Random
        list is fresh per browse. Starred is deliberately omitted (it has its
        own Starred vdir).
        """
        return [
            Ref.directory(name=name, uri=uri.get_list_uri(ltype))
            for name, ltype in _ALBUM_LISTS
        ]

    def browse_artist(self, artist_id):
        """Browse an artist: two radio entries, then the albums.

        Fetches getArtist exactly ONCE and derives both the album refs and the
        artist name from that single response (no second round-trip). The two
        radio dirs (Similar Songs, Top Songs) lead the listing deterministically
        so the ordering is stable across servers; the albums follow, already
        name-sorted by the subsonic_api helper.

        - Similar Songs: getSimilarSongs2(artist_id), id-based, always shown.
        - Top Songs: getTopSongs(artist_name), name-based - only shown when the
          artist name is known (else the entry is skipped rather than issuing
          getTopSongs with an empty name).

        On a failed getArtist the name is None (Top Songs skipped) and the
        album list is empty; Similar Songs still resolves off the id. Never
        raises - the underlying helpers degrade to empty lists.
        """
        raw_artist = self.subsonic_api.get_raw_artist(artist_id)
        artist_name = raw_artist.get("name") if raw_artist else None
        radio_refs = [
            Ref.directory(
                name="Similar Songs", uri=uri.get_similar_uri(artist_id)
            )
        ]
        if artist_name:
            radio_refs.append(
                Ref.directory(
                    name="Top Songs", uri=uri.get_top_uri(artist_name)
                )
            )
        album_refs = self.subsonic_api.get_albums_as_refs_from_raw(raw_artist)
        return radio_refs + album_refs

    def browse_album(self, album_id):
        """Browse an album: a Similar Songs radio entry, then the songs.

        getSimilarSongs2 accepts an album id, so the same helper delivers the
        goal's per-album instant-mix. The radio entry leads the listing; the
        album's own songs follow.
        """
        radio_ref = Ref.directory(
            name="Similar Songs", uri=uri.get_similar_uri(album_id)
        )
        return [radio_ref] + self.browse_songs(album_id)

    def browse_starred(self):
        """Read-only mirror of the server's starred content.

        Concatenates starred artists, albums and songs as refs. All three come
        from a single cached getStarred2 payload (see fetch_starred), so this
        is one round-trip, not three. Browsing an album/artist ref from here
        reuses the existing ARTIST/ALBUM browse branches unchanged. On network
        failure the underlying helpers yield empty lists, so the dir is simply
        empty - never an error.
        """
        return self.subsonic_api.get_starred_as_refs()

    def browse_diritems(self, directory_id):
        return self.subsonic_api.get_diritems_as_refs(directory_id)

    def lookup_song(self, song_id):
        song = self.subsonic_api.get_song_by_id(song_id)
        if song is None:
            return []
        else:
            return [song]

    def lookup_album(self, album_id):
        return self.subsonic_api.get_songs_as_tracks(album_id)

    def lookup_artist(self, artist_id):
        return list(
            self.subsonic_api.get_artist_as_songs_as_tracks_iter(artist_id)
        )

    def lookup_directory(self, directory_id):
        return list(
            self.subsonic_api.get_recursive_dir_as_songs_as_tracks_iter(
                directory_id
            )
        )

    def lookup_playlist(self, playlist_id):
        return self.subsonic_api.get_playlist_as_playlist(playlist_id).tracks

    def browse(self, browse_uri):
        if browse_uri == uri.get_vdir_uri("root"):
            root_vdir_names = [
                "rootdirs",
                "artists",
                "albums",
                "radio",
                "lists",
                "genres",
                subsonic_api.RESERVED_STARRED_ID,
            ]
            root_vdirs = [
                self._vdirs[vdir_name] for vdir_name in root_vdir_names
            ]
            sorted_root_vdirs = sorted(
                root_vdirs, key=lambda vdir: vdir["name"]
            )
            return [self._raw_vdir_to_ref(vdir) for vdir in sorted_root_vdirs]
        elif browse_uri == uri.get_vdir_uri("rootdirs"):
            return self.browse_rootdirs()
        elif browse_uri == uri.get_vdir_uri("artists"):
            return self.browse_artists()
        elif browse_uri == uri.get_vdir_uri("albums"):
            return self.browse_albums()
        elif browse_uri == uri.get_vdir_uri("radio"):
            return self.browse_radio()
        elif browse_uri == uri.get_vdir_uri("lists"):
            return self.browse_lists()
        elif browse_uri == uri.get_vdir_uri("genres"):
            return self.browse_genres()
        elif browse_uri == uri.get_vdir_uri("random"):
            return self.browse_random_songs()
        elif browse_uri == uri.get_vdir_uri(subsonic_api.RESERVED_STARRED_ID):
            return self.browse_starred()

        else:
            uri_type = uri.get_type(browse_uri)
            if uri_type == uri.DIRECTORY:
                return self.browse_diritems(uri.get_directory_id(browse_uri))
            elif uri_type == uri.ARTIST:
                return self.browse_artist(uri.get_artist_id(browse_uri))
            elif uri_type == uri.ALBUM:
                return self.browse_album(uri.get_album_id(browse_uri))
            elif uri_type == uri.SIMILAR:
                # A hand-crafted bare 'subidy:similar:' yields a None id; skip
                # the fetch rather than call getSimilarSongs2 with None.
                similar_id = uri.get_similar_id(browse_uri)
                if similar_id is None:
                    return []
                return self.subsonic_api.get_similar_songs_as_refs(similar_id)
            elif uri_type == uri.TOP:
                # Same guard for a bare 'subidy:top:' (None artist name).
                top_id = uri.get_top_id(browse_uri)
                if top_id is None:
                    return []
                return self.subsonic_api.get_top_songs_as_refs(top_id)
            elif uri_type == uri.LIST:
                # Guard a bare 'subidy:list:' (None) and any hand-crafted
                # arbitrary ltype before it reaches the server; only the known
                # tokens in _ALBUM_LISTS are ever passed to getAlbumList2.
                list_type = uri.get_list_type(browse_uri)
                if list_type not in _VALID_LIST_TYPES:
                    return []
                return self.subsonic_api.get_album_list_as_refs(list_type)
            elif uri_type == uri.GENRE:
                # A hand-crafted bare 'subidy:genre:' yields a None name; skip
                # the fetch rather than call getSongsByGenre with None.
                genre_name = uri.get_genre_name(browse_uri)
                if genre_name is None:
                    return []
                return self.subsonic_api.get_songs_by_genre_as_refs(genre_name)
            else:
                return []

    def lookup_one(self, lookup_uri):
        type = uri.get_type(lookup_uri)
        if type == uri.ARTIST:
            return self.lookup_artist(uri.get_artist_id(lookup_uri))
        if type == uri.ALBUM:
            return self.lookup_album(uri.get_album_id(lookup_uri))
        if type == uri.DIRECTORY:
            return self.lookup_directory(uri.get_directory_id(lookup_uri))
        if type == uri.SONG:
            return self.lookup_song(uri.get_song_id(lookup_uri))
        if type == uri.PLAYLIST:
            return self.lookup_playlist(uri.get_playlist_id(lookup_uri))

    def lookup(self, uri=None, uris=None):
        # lookup_one returns None for a directory-only uri type (e.g. SIMILAR/
        # TOP radio dirs, which have no track lookup); mopidy's contract is
        # that lookup returns a list, so coerce None to [].
        if uris is not None:
            return {uri: (self.lookup_one(uri) or []) for uri in uris}
        if uri is not None:
            return self.lookup_one(uri) or []
        return []

    def get_images(self, uris):
        """Return cover-art images for song/album/artist URIs.

        Maps each input URI to a list of mopidy Images (empty for URIs with
        no art or of an unsupported type). The result is keyed by the exact
        input URI, as mopidy core rejects any other key. Runs on the backend
        actor thread and must never raise, so each URI is guarded: a failure
        yields [] for that URI and leaves playback unaffected.
        """
        result = {}
        for u in uris:
            try:
                result[u] = self._images_for_uri(u)
            except Exception:
                logger.warning("Failed to resolve cover art for %s", u)
                result[u] = []
        return result

    def _images_for_uri(self, u):
        uri_type = uri.get_type(u)
        if uri_type not in (uri.SONG, uri.ALBUM, uri.ARTIST):
            return []
        cover_art_id = self.subsonic_api.get_cover_art_id_for_uri(u)
        if not cover_art_id:
            return []
        url = self.subsonic_api.get_cover_art_url(cover_art_id)
        if not url:
            return []
        size = subsonic_api.DEFAULT_IMAGE_SIZE
        return [Image(uri=url, width=size, height=size)]

    def refresh(self, uri):
        pass

    def search_by_artist_album_and_track(
        self, artist_name, album_name, track_name
    ):
        tracks = self.search_by_artist_and_album(artist_name, album_name)
        track = next(item for item in tracks.tracks if track_name in item.name)
        return SearchResult(tracks=[track])

    def search_by_artist_and_album(self, artist_name, album_name):
        artists = self.subsonic_api.find_raw(artist_name).get("artist")
        if artists is None:
            return None
        tracks = []
        for artist in artists:
            for album in self.subsonic_api.get_raw_albums(artist.get("id")):
                if album_name in album.get("name"):
                    tracks.extend(
                        self.subsonic_api.get_songs_as_tracks(album.get("id"))
                    )
        return SearchResult(tracks=tracks)

    def search_by_artist(self, artist_name, exact):
        result = self.subsonic_api.find_raw(artist_name)
        if result is None:
            return None
        tracks = []
        for artist in result.get("artist"):
            if exact:
                if not artist.get("name") == artist_name:
                    continue

            tracks.extend(
                self.subsonic_api.get_artist_as_songs_as_tracks_iter(
                    artist.get("id")
                )
            )
        return SearchResult(uri=uri.get_search_uri(artist_name), tracks=tracks)

    def get_distinct(self, field, query):
        # `list genre` (MPD) is query-independent: return the full genre set
        # from getGenres, before any search (which would be empty for a bare
        # `list genre` and short-circuit to []).
        if field == "genre":
            return [ref.name for ref in self.browse_genres()]
        search_result = self.search(query)
        if not search_result:
            return []
        if field == "track" or field == "title":
            return [track.name for track in (search_result.tracks or [])]
        if field == "album":
            return [album.name for album in (search_result.albums or [])]
        if field == "artist":
            if not search_result.artists:
                return [artist.name for artist in self.browse_artists()]
            return [artist.name for artist in search_result.artists]

    def search(self, query=None, uris=None, exact=False):
        if "artist" in query and "album" in query and "track_name" in query:
            return self.search_by_artist_album_and_track(
                query.get("artist")[0],
                query.get("album")[0],
                query.get("track_name")[0],
            )
        if "artist" in query and "album" in query:
            return self.search_by_artist_and_album(
                query.get("artist")[0], query.get("album")[0]
            )
        if "artist" in query:
            return self.search_by_artist(query.get("artist")[0], exact)
        if "comment" in query:
            if query.get("comment")[0] == "random":
                return SearchResult(
                    tracks=self.subsonic_api.get_random_songs_as_tracks()
                )
        if "any" in query:
            return self.subsonic_api.find_as_search_result(query.get("any")[0])
        return SearchResult(artists=self.subsonic_api.get_artists_as_artists())
