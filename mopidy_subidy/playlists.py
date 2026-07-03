import logging

from mopidy import backend
from mopidy.models import Playlist, Ref
from mopidy_subidy import subsonic_api, uri

logger = logging.getLogger(__name__)

# The virtual "Starred" playlist gives MPD clients (ncmpcpp) a real star
# action: MPD has no native star verb, but adding/removing a track in this
# playlist and saving diffs the desired set against the server's starred set,
# starring newly-added tracks and unstarring removed ones. It is idempotent -
# re-saving an unchanged set issues zero network writes.
STARRED_NAME = "Starred"
STARRED_URI = uri.get_playlist_uri(subsonic_api.RESERVED_STARRED_ID)


class SubidyPlaylistsProvider(backend.PlaylistsProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subsonic_api = self.backend.subsonic_api
        self.playlists = []
        self.refresh()

    def _is_starred_uri(self, playlist_uri):
        return (
            uri.get_playlist_id(playlist_uri)
            == subsonic_api.RESERVED_STARRED_ID
        )

    def as_list(self):
        # Prepend the virtual Starred ref. Prepending (rather than appending)
        # means it stays visible even when the server playlist listing fails
        # and returns [], and it wins the clean "Starred" name if a real server
        # playlist happens to share it (mopidy-mpd uniquifies later refs).
        starred_ref = Ref.playlist(uri=STARRED_URI, name=STARRED_NAME)
        return [starred_ref] + self.subsonic_api.get_playlists_as_refs()

    def create(self, name):
        # Unchanged: only real server playlists are created. A real playlist
        # named "Starred" is harmless - the virtual one is matched by its
        # reserved uri in lookup/save/delete and stays reachable by its own id.
        result = self.subsonic_api.create_playlist_raw(name)
        if result is None:
            return None
        playlist = result.get("playlist")
        if playlist is None:
            for pl in self.subsonic_api.get_playlists_as_playlists():
                if pl.name == name:
                    playlist = pl
            return playlist
        else:
            return self.subsonic_api.raw_playlist_to_playlist(playlist)

    def delete(self, playlist_uri):
        if self._is_starred_uri(playlist_uri):
            # The virtual playlist is not a real server playlist; refuse to
            # delete it rather than issuing a bogus deletePlaylist. Contract
            # is delete(uri) -> bool, so report failure.
            logger.warning("The virtual Starred playlist cannot be deleted.")
            return False
        playlist_id = uri.get_playlist_id(playlist_uri)
        result = self.subsonic_api.delete_playlist_raw(playlist_id)
        return result is not None

    def get_items(self, items_uri):
        # Contract: return list[Ref.track]. The starred branch returns refs
        # (not tracks) built from the same cached getStarred2 payload.
        if self._is_starred_uri(items_uri):
            return self.subsonic_api.get_starred_songs_as_refs()
        return self.subsonic_api.get_playlist_as_songs_as_refs(
            uri.get_playlist_id(items_uri)
        )

    def lookup(self, lookup_uri):
        if self._is_starred_uri(lookup_uri):
            return Playlist(
                uri=STARRED_URI,
                name=STARRED_NAME,
                tracks=self.subsonic_api.get_starred_songs_as_tracks(),
            )
        return self.subsonic_api.get_playlist_as_playlist(
            uri.get_playlist_id(lookup_uri)
        )

    def refresh(self):
        pass

    def save(self, playlist):
        if self._is_starred_uri(playlist.uri):
            return self._save_starred(playlist)
        playlist_id = uri.get_playlist_id(playlist.uri)
        track_ids = []
        for trk in playlist.tracks:
            track_ids.append(uri.get_song_id(trk.uri))
        result = self.subsonic_api.save_playlist_raw(playlist_id, track_ids)
        if result is None:
            return None
        return playlist

    def _save_starred(self, playlist):
        """Diff the saved Starred playlist against the server's starred set.

        Stars songs added, unstars songs removed. Song-only: album/artist stars
        are browse-visible under the Starred dir but cannot be set through this
        track playlist (mopidy models a playlist as tracks). Foreign-backend or
        non-song tracks dragged in are ignored, not crashed on.

        Fail-safe against a lossy read: the current starred set is fetched via
        fetch_starred(), which returns None on network failure (distinct from
        an authentic empty set). If that read fails we ABORT the diff and return
        the playlist unchanged, so a flaky link never turns a read failure into
        a mass unstar.

        Empty-desired guard: an MPD `playlistclear Starred` (or a save from a
        queue with no subidy songs) yields desired == {}. We treat that as a
        no-op for unstarring rather than wiping the entire starred library,
        which would be unrecoverable. Removing the LAST starred song via this
        playlist is therefore intentionally not supported; unstar it from the
        Starred browse dir's source or another client instead.
        """
        # Guard every track: a Track with uri None, a foreign-backend uri, or a
        # non-song subidy uri (album/artist dragged in) must never reach
        # uri.get_type/get_song_id in a way that raises or contributes a bogus
        # id. uri.get_type(None) itself would raise, hence the leading t.uri.
        desired = {
            uri.get_song_id(t.uri)
            for t in playlist.tracks
            if t.uri
            and uri.get_type(t.uri) == uri.SONG
            and uri.get_song_id(t.uri)
        }
        # Single getStarred2 read for this operation. None means the read
        # FAILED (network/API) - an unknown baseline - and we must never diff
        # against it, else a transient failure could mass-unstar. {} means the
        # server genuinely has nothing starred.
        starred = self.subsonic_api.fetch_starred()
        if starred is None:
            logger.warning(
                "Not saving Starred playlist: could not read current starred "
                "set from subsonic (network failure). No stars were changed."
            )
            return Playlist(
                uri=STARRED_URI, name=STARRED_NAME, tracks=tuple()
            )
        current = {
            str(song.get("id"))
            for song in subsonic_api._as_list(starred.get("song"))
        }
        to_star = desired - current
        to_unstar = current - desired
        # Data-loss guard: an empty desired set (playlistclear / an empty save)
        # must never wipe the entire starred library. Refuse to unstar in that
        # case; starring on a non-empty save is still fine.
        if not desired and to_unstar:
            logger.warning(
                "Refusing to unstar all songs from an empty Starred playlist "
                "save (%d songs). Remove individual tracks to unstar them.",
                len(to_unstar),
            )
            to_unstar = set()
        # Track outcomes so a partial failure (one of two writes) is visible
        # rather than silently dropped.
        star_ok = unstar_ok = None
        if to_star:
            star_ok = self.subsonic_api.star(song_ids=list(to_star))
            if star_ok:
                logger.info(
                    "Starred %d song(s) via Starred playlist.", len(to_star)
                )
        if to_unstar:
            unstar_ok = self.subsonic_api.unstar(song_ids=list(to_unstar))
            if unstar_ok:
                logger.info(
                    "Unstarred %d song(s) via Starred playlist.", len(to_unstar)
                )
        if to_star and to_unstar and not (star_ok and unstar_ok):
            logger.warning(
                "Partial Starred save: star of %d song(s) %s, unstar of %d "
                "song(s) %s.",
                len(to_star),
                "succeeded" if star_ok else "FAILED",
                len(to_unstar),
                "succeeded" if unstar_ok else "FAILED",
            )
        if not to_star and not to_unstar:
            # No writes: the read we already did is authoritative and fresh, so
            # build the returned view from it without a second getStarred2.
            return Playlist(
                uri=STARRED_URI,
                name=STARRED_NAME,
                tracks=tuple(
                    self.subsonic_api.raw_song_to_track(song)
                    for song in subsonic_api._as_list(starred.get("song"))
                ),
            )
        # A write happened; invalidate so the returned playlist reflects the
        # just-applied change (one extra getStarred2, strictly needed here).
        self.subsonic_api.invalidate_starred_cache()
        return self.lookup(playlist.uri)
