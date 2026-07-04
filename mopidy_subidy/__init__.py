import pathlib

import pkg_resources

from mopidy import config, ext

__version__ = pkg_resources.get_distribution("Mopidy-Subidy").version


class SubidyExtension(ext.Extension):

    dist_name = "Mopidy-Subidy"
    ext_name = "subidy"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        schema["url"] = config.String()
        schema["username"] = config.String()
        schema["password"] = config.Secret()
        schema["legacy_auth"] = config.Boolean(optional=True)
        schema["api_version"] = config.String(optional=True)
        schema["scrobbling"] = config.Boolean(optional=True)
        # Number of songs the algorithmic-radio surfaces return per browse
        # (Random Songs, per-artist Similar/Top Songs, per-album Similar).
        # Subsonic's getRandomSongs caps at 500. Integer(optional=True, ...)
        # REJECTS an out-of-range value at load (it does not clamp); ext.conf
        # ships a default of 50, so the value is effectively always present.
        schema["radio_size"] = config.Integer(
            optional=True, minimum=1, maximum=500
        )
        # Number of albums each smart-list surface (Most Played, Recently
        # Added, ...) returns per browse. getAlbumList2 caps size at 500;
        # Integer(optional=True, ...) REJECTS an out-of-range value at load (it
        # does not clamp). ext.conf ships a default of 100, so the value is
        # effectively always present. Distinct from radio_size (albums vs
        # songs), hence the larger default.
        schema["album_list_size"] = config.Integer(
            optional=True, minimum=1, maximum=500
        )
        # Number of songs each genre surface returns per browse. getSongsByGenre
        # caps count at 500; Integer(optional=True, ...) REJECTS an out-of-range
        # value at load (it does not clamp). ext.conf ships a default of 100, so
        # the value is effectively always present.
        schema["genre_songs_size"] = config.Integer(
            optional=True, minimum=1, maximum=500
        )
        # Seconds a browse listing (getArtists / rootdirs / dir listings /
        # smart-list album pages / genres) is cached to collapse the repeated
        # round-trips a single browse fan-out issues. 0 disables the listing
        # cache entirely (every browse re-fetches). Integer(optional=True, ...)
        # REJECTS a negative at load; ext.conf ships a default of 30. This
        # cache never holds starred/random/search data, so the knob only
        # affects static-listing staleness (and the short lag of the
        # play/rating-driven smart-lists).
        schema["listing_cache_ttl"] = config.Integer(optional=True, minimum=0)
        return schema

    def setup(self, registry):
        from .backend import SubidyBackend
        from .frontend import SubidyScrobblerFrontend

        registry.add("backend", SubidyBackend)
        registry.add("frontend", SubidyScrobblerFrontend)
