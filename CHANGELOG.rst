*********
Changelog
*********


Unreleased
==========

- Add algorithmic radio / instant-mix as browsable, queue-able content:

  - A top-level ``Radio`` browse dir with a ``Random Songs`` child backed by
    ``getRandomSongs`` (random per browse, never cached).
  - Per-artist ``Similar Songs`` (``getSimilarSongs2``) and ``Top Songs``
    (``getTopSongs``) entries when browsing an artist.
  - Per-album ``Similar Songs`` instant-mix (``getSimilarSongs2`` on the album
    id) when browsing an album.

  Every entry yields real, playable ``subidy:song`` refs. Network failures are
  logged and yield an empty dir - playback is never interrupted.

- Add a ``radio_size`` config key (default 50, range 1-500) governing how many
  songs the radio surfaces return. This also now governs the random-songs
  browse (previously a hardcoded 75) and the ``comment=random`` search
  (previously up to 500), unifying them onto one knob.


v1.0.0 (2020-03-13)
===================

- Require Mopidy 3.0 or newer.

- Update extension to match the Mopidy extension cookiecutter.


v0.4.1 (2020-02-01)
===================

- Require Python 3.7 or newer.

- Require py-sonic 0.7.7 or newer.


v0.4.0 (2017-08-14)
===================

- Use Mopidy extension name as Subsonic API app name.


v0.3.4 (2017-06-12)
===================

- Playlist improvements.


v0.3.3 (2017-05-15)
===================

- Add API version setting.


v0.3.2 (2017-05-04)
===================

- Fix playlist track listing.


v0.3.1 (2017-03-23)
===================

- Fix URL encoding bug.


v0.3.0 (2017-03-22)
===================

- Add support for browsing.


v0.2.7 (2017-03-14)
===================

- Improved sorting of results.


v0.2.6 (2017-03-04)
===================

- Require py-sonic 0.6.1 to support legacy auth.


v0.2.5 (2017-02-27)
===================

- Fix legacy auth support.


v0.2.4 (2017-02-23)
===================

- Document current features/restrictions.

- Fix bug.


v0.2.3 (2016-11-03)
===================

- Add more debug logging.


v0.2.2 (2016-11-02)
===================

- Improved error handling.


v0.2.1 (2016-09-22)
===================

- Improved search.


v0.2.0 (2016-09-22)
===================

- Add basic naive search.


v0.1.1 (2016-09-20)
===================

- Initial release.
