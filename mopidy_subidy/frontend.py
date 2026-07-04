import logging

import pykka

import mopidy_subidy
from mopidy import core
from mopidy_subidy import subsonic_api, uri

logger = logging.getLogger(__name__)

# AudioScrobbler rule: submit a completed play once the track has been played
# for at least half its length or four minutes, whichever comes first.
SCROBBLE_CAP_MS = 240 * 1000

# AudioScrobbler rule: never submit a completed play for tracks shorter than
# 30 seconds.
SCROBBLE_MIN_LENGTH_MS = 30 * 1000


class SubidyScrobblerFrontend(pykka.ThreadingActor, core.CoreListener):
    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.enabled = config["subidy"]["scrobbling"]
        subidy_config = config["subidy"]
        # Build our own SubsonicApi from config, mirroring backend.py, so the
        # frontend can call scrobble() directly as a plain synchronous method
        # rather than reaching into the backend actor through a pykka proxy.
        self.subsonic_api = subsonic_api.SubsonicApi(
            url=subidy_config["url"],
            username=subidy_config["username"],
            password=subidy_config["password"],
            app_name=mopidy_subidy.SubidyExtension.dist_name,
            legacy_auth=subidy_config["legacy_auth"],
            api_version=subidy_config["api_version"],
            # The scrobbler frontend only submits nowPlaying/scrobble and never
            # browses, so its SubsonicApi never touches the listing cache.
            # Passing 0 makes _TtlLru a no-op (get always misses, set is a
            # no-op) so no browse-cache memory is held for this instance.
            listing_cache_ttl=0,
        )

    def on_start(self):
        if not self.enabled:
            logger.info("Subidy scrobbling is disabled.")

    def _scrobble(self, uri_str, submission):
        if not self.enabled:
            return
        song_id = uri.get_song_id(uri_str)
        if song_id is None:
            return
        try:
            self.subsonic_api.scrobble(song_id, submission=submission)
        except Exception:
            logger.warning(
                "Subidy scrobbling failed for %s.", uri_str, exc_info=True
            )

    def track_playback_started(self, tl_track):
        if tl_track is None or tl_track.track is None:
            return
        self._scrobble(tl_track.track.uri, submission=False)

    def track_playback_ended(self, tl_track, time_position):
        if tl_track is None or tl_track.track is None:
            return
        track = tl_track.track
        length = track.length or 0
        # Never submit a completed play for tracks shorter than 30 seconds.
        if length < SCROBBLE_MIN_LENGTH_MS:
            return
        threshold = min(length // 2, SCROBBLE_CAP_MS)
        if time_position >= threshold:
            self._scrobble(track.uri, submission=True)
