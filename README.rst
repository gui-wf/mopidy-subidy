*************
Mopidy-Subidy
*************

.. image:: https://img.shields.io/pypi/v/Mopidy-Subidy
    :target: https://pypi.org/project/Mopidy-Subidy/
    :alt: Latest PyPI version

.. image:: https://img.shields.io/circleci/build/gh/Prior99/mopidy-subidy
    :target: https://circleci.com/gh/Prior99/mopidy-subidy
    :alt: CircleCI build status

.. image:: https://img.shields.io/codecov/c/gh/Prior99/mopidy-subidy
    :target: https://codecov.io/gh/Prior99/mopidy-subidy
    :alt: Test coverage

**This library is actively looking for maintainers to help out as I do not have the time or need to maintain this anymore. Please contact me if you feel that you could maintain this.**

A Subsonic backend for Mopidy using `py-sonic
<https://github.com/crustymonkey/py-sonic>`_.


Installation
============

Install the latest release from PyPI by running::

    python3 -m pip install Mopidy-Subidy

Install the development version directly from this repo by running::

    python3 -m pip install https://github.com/Prior99/mopidy-subidy/archive/master.zip

See https://mopidy.com/ext/subidy/ for alternative installation methods.


Configuration
=============

Before starting Mopidy, you must add configuration for Mopidy-Subidy to your
Mopidy configuration file::

   [subidy]
   url=https://path.to/your/subsonic/server
   username=subsonic_username
   password=your_secret_password

In addition, the following optional configuration values are supported:

- ``enabled`` -- Defaults to ``true``. Set to ``false`` to disable the
  extension.

- ``legacy_auth`` -- Defaults to ``false``. Setting to ``true`` may solve some
  connection errors.

- ``api_version`` -- Defaults to ``1.14.0``, which is the version used by
  Subsonic 6.2.

- ``scrobbling`` -- Defaults to ``true``. Set to ``false`` to disable
  submitting plays to the Subsonic server. When enabled, Mopidy-Subidy sends a
  "now playing" notification when a track starts and submits a completed play
  once the track has been played for at least half its length or four minutes,
  whichever comes first (the standard AudioScrobbler rule).


Starred / loved content
=======================

Mopidy-Subidy surfaces your Subsonic starred (loved) content in two ways, and
lets MPD clients such as ncmpcpp star tracks even though MPD has no native
"star" command:

- A top-level **Starred** browse directory (under the Subsonic root) that lists
  your starred artists, albums and tracks, populated via ``getStarred2``. This
  is a read-only mirror.

- A virtual editable **Starred** playlist. Adding a track to this playlist and
  saving it stars the track on the server; removing a track and saving unstars
  it. Saving diffs the playlist against the server's current starred set, so
  re-saving an unchanged playlist makes no server changes.

Important behaviour and limits:

- **Songs only.** The Starred *playlist* trigger stars/unstars songs only.
  Starred albums and artists are visible in the Starred *browse directory* but
  cannot be starred or unstarred through the playlist (Mopidy models a playlist
  as a list of tracks). Star albums/artists from another Subsonic client.

- **Clearing the Starred playlist does not mass-unstar.** Saving an empty
  Starred playlist (e.g. ``playlistclear Starred``) is treated as a no-op for
  unstarring rather than removing every star, since that would be
  unrecoverable. Remove individual tracks and save to unstar them.

- **Resilient.** If the server is unreachable, the Starred dir/playlist appear
  empty and saving changes nothing; playback is never interrupted. In
  particular, a failed read of the current starred set aborts a save instead of
  risking a destructive diff.

No configuration is required; the feature relies only on the standard
``star``/``unstar``/``getStarred2`` endpoints (Subsonic API 1.8.0+), well below
the default ``api_version``.


State of this plugin
====================

The following things are supported:

- Browsing all artists/albums/tracks
- Searching for any terms
- Browsing, creating, editing and deleting playlists
- Searching explicitly for one of: artists, albums, tracks
- Browsing starred content and starring/unstarring tracks (see above)

The following things are **not** supported:

- Subsonic's smart playlists
- Searching for a combination of filters (artist and album, artist and track, etc.)
- Starring albums/artists via the MPD playlist trigger (browse-only)


Credits
=======

- Original author: `Frederick Gnodtke <https://github.com/Prior99>`__
- Current maintainer: `Frederick Gnodtke <https://github.com/Prior99>`__
- `Contributors <https://github.com/Prior99/mopidy-subidy/graphs/contributors>`_
