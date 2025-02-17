import json
import optparse
import os.path
import re
from datetime import datetime

import backoff
import cachetools
import confuse
import tidalapi

from beets import ui
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.importer import ImportSession, ImportTask
from beets.library import Library
from beets.plugins import BeetsPlugin
from beets.util import bytestring_path, remove, syspath


def backoff_handler(details):
    """Handler for rate limiting backoff"""
    TidalPlugin.logger.debug(
        "Rate limited! Cooling off for {wait:0.1f} seconds after calling function {target.__name__} {tries} times".format(
            **details
        )
    )


class TidalPlugin(BeetsPlugin):
    """TidalPlugin is a TIDAL source for the autotagger"""

    # The built-in beets logger is an instance variable, so to access it
    # in the backoff_handler, we have to assign it to a static variable.
    logger = None

    data_source = "tidal"
    track_share_regex = r"(tidal.com\/browse\/track\/)([0-9]*)(\?u)"  # Format: https://tidal.com/browse/track/221182395?u
    album_share_regex = r"(tidal.com\/browse\/album\/)([0-9]*)(\?u)"  # Format: https://tidal.com/browse/album/221182592?u
    # Number of times to retry when we get a TooManyRequests exception, this implements an exponential backoff.
    rate_limit_retries = 16

    def __init__(self):
        super().__init__()
        TidalPlugin.logger = self._log
        self.import_stages = [self.stage]

        # Import config
        # The lyrics search limit is much smaller than the metadata search limit
        # as the current implementation is very API-heavy and TIDAL heavily
        # rate limits the lyrics API so increasing the limit causes
        # an __exponential__ increase in API calls.
        self.config.add(
            {
                "auto": True,
                "lyrics": True,
                "synced_lyrics": True,
                "overwrite_lyrics": True,
                "metadata_search_limit": 25,  # Search limit when querying API for metadata
                "lyrics_search_limit": 10,  # Search limit when querying API for lyrics
                "tokenfile": "tidal_token.json",
                "write_sidecar": False,  # Write lyrics to LRC file
            }
        )

        self.sessfile = self.config["tokenfile"].get(
            confuse.Filename(in_app_dir=True)
        )

        # tidalapi.session.Session object we throw around to execute API calls with
        self.sess = None

    def _load_session(self, fatal=False):
        """Loads a TIDAL session from a JSON file to the class singleton

        :param fatal: Toggles if login failures result in UserError, defaults to False
        :type fatal: bool, optional
        :raises ui.UserError: Raised when login fails
        :return: If the login was successful or not, only if fatal is False.
        :rtype: bool
        """
        if self.sess:
            self._log.debug(
                "Not attempting to load session state as we already have a session!"
            )
            return True

        self._log.debug(
            f"Attempting to load session state from {self.sessfile}!"
        )
        self.sess = tidalapi.session.Session()

        # Attempt to load OAuth data from token file
        try:
            with open(self.sessfile) as file:
                sess_data = json.load(file)
        except (OSError, json.JSONDecodeError):
            # Error occured, most likely token file does not exist.
            self._log.debug("Session state file does not exist or is corrupt")
            if fatal:
                raise ui.UserError(
                    "Please login to TIDAL using `beets tidal --login`"
                )
            else:
                return False
        else:
            # Got some JSON data from the file
            # Let's load the data into a session and check for validity.
            self.sess.load_oauth_session(
                sess_data["token_type"],
                sess_data["access_token"],
                sess_data["refresh_token"],
                datetime.fromisoformat(sess_data["expiry_time"]),
            )

            if not self.sess.check_login():
                self._log.debug(
                    "Session state loaded but check_login() returned False"
                )

                # Clear session file so we don't keep spamming the API
                with open(self.sessfile, "w") as file:
                    self._log.debug(
                        "Clearing session state file to avoid unneeded API calls"
                    )

                    remove(bytestring_path(self.sessfile), soft=True)

                if fatal:
                    raise ui.UserError(
                        "Please login to TIDAL using `beets tidal --login`"
                    )
                else:
                    return False

            return True

    def _save_session(self, sess):
        """Saves a TIDAL session to a JSON file

        :param sess: Session to save
        :type sess: tidalapi.session.Session
        """
        self._log.debug(f"Saving session state to {self.sessfile}!")
        with open(self.sessfile, "w") as file:
            json.dump(
                {
                    "token_type": sess.token_type,
                    "access_token": sess.access_token,
                    "refresh_token": sess.refresh_token,
                    "expiry_time": sess.expiry_time.isoformat(),
                },
                file,
            )

    def _login(self):
        """Creates a session to use with the TIDAL API

        :raises ui.UserError: Raised when login fails
        """
        self.sess = tidalapi.session.Session()
        login, future = self.sess.login_oauth()
        ui.print_(
            f"Open the following URL to complete login: https://{login.verification_uri_complete}"
        )
        ui.print_(f"The link expires in {int(login.expires_in)} seconds!")

        if not future.result():
            raise ui.UserError("Login failure! See above output for more info.")

        self._save_session(self.sess)

    def cmd_main(self, lib: Library, opts: optparse.Values, arg: list):
        if opts.login:
            self._log.debug("Running login routine!")
            self._login()
        elif opts.dump_sess:
            self._log.debug(f"Session state file: {self.sessfile}")
            try:
                with open(self.sessfile) as file:
                    sess_data = json.load(file)
                    print(sess_data)
            except OSError:
                self._log.info(
                    f"Session state file {self.sessfile} does not exist!"
                )

    def commands(self):
        cmd = ui.Subcommand("tidal", help="fetch metadata from TIDAL")
        cmd.parser.add_option(
            "-l",
            "--login",
            dest="login",
            action="store_true",
            default=False,
            help="login to TIDAL",
        )

        cmd.parser.add_option(
            "-d",
            "--dump",
            dest="dump_sess",
            action="store_true",
            default=False,
            help="dump session state",
        )

        cmd.func = self.cmd_main
        return [cmd]

    def album_for_id(self, album_id):
        """Return TIDAL metadata for a specific TIDAL Album ID

        :param album_id: A user provided ID obtained from the tagger prompt
        :type album_id: str
        :return: AlbumInfo for the given ID if found, otherwise Nothing.
        :rtype: beets.autotag.hooks.AlbumInfo or None
        """
        # Check for session
        self._log.debug(f"Running album_for_id with track {album_id}!")
        if not self._load_session():
            self._log.info(
                "Skipping album_for_id because we have no session! Please login."
            )
            return None

        # This is just the numerical album ID to use with the TIDAL API
        tidal_album_id = None

        # Try to use album_id directly, otherwise parse it from URL
        try:
            tidal_album_id = int(album_id)
            self._log.debug("Using track_id directly in album_for_id")
        except ValueError:
            self._log.debug("album_id is NOT an integer, parsing it with regex")
            regx = re.search(self.album_share_regex, album_id)
            if not regx:
                self._log.debug("Regex returned no matches")
                return None

            if len(regx.groups()) != 3:
                self._log.debug(
                    f"Album share URL parsing failed because we got {len(regx.groups())} groups when 3 was expected"
                )
                return None

            tidal_album_id = int(regx.groups()[-2])

        try:
            album = self.sess.album(tidal_album_id)
        except tidalapi.exceptions.ObjectNotFound:
            self._log.debug(f"No album for ID {tidal_album_id}")
            return None

        return self._album_to_albuminfo(album)

    def track_for_id(self, track_id):
        """Return TIDAL metadata for a specific TIDAL Track ID

        :param track_id: A user provided ID obtained from the tagger prompt
        :type track_id: str
        :return: TrackInfo for the given ID if found, otherwise Nothing.
        :rtype: beets.autotag.hooks.TrackInfo or None
        """
        # Check for session
        self._log.debug(f"Running track_for_id with track {track_id}!")
        if not self._load_session():
            self._log.info(
                "Skipping track_for_id because we have no session! Please login."
            )
            return None

        # This is just the numerical track ID to use with the TIDAL API
        tidal_track_id = None

        # Try to use track_id directly, otherwise parse it from URL
        try:
            tidal_track_id = int(track_id)
            self._log.debug("Using track_id directly in track_for_id")
        except ValueError:
            self._log.debug("track_id is NOT an integer, parsing it with regex")
            regx = re.search(self.track_share_regex, track_id)
            if not regx:
                self._log.debug("Regex returned no matches")
                return None

            if len(regx.groups()) != 3:
                self._log.debug(
                    f"Track share URL parsing failed because we got {len(regx.groups())} groups when 3 was expected"
                )
                return None

            tidal_track_id = int(regx.groups()[-2])

        try:
            track = self.sess.track(tidal_track_id, with_album=True)
        except tidalapi.exceptions.ObjectNotFound:
            self._log.debug(f"No track for ID {tidal_track_id}")
            return None

        return self._track_to_trackinfo(track, track.album)

    def candidates(self, items, artist, album, va_likely, extra_tags):
        """Returns TIDAL metadata candidates for a specific set of items, typically an album"""
        if not self._load_session():
            self._log.info(
                "Skipping candidates because we have no session! Please login."
            )
            return []

        candidates = []

        self._log.debug(
            "Searching for candidates using tidal_album_id from items"
        )
        for item in items:
            if item.get("tidal_album_id", None):
                try:
                    album = self._album_to_albuminfo(
                        self.sess.album(item.tidal_album_id)
                    )
                    candidates.append(album)
                except tidalapi.exceptions.ObjectNotFound:
                    self._log.debug(
                        f"No album found for ID {item.tidal_album_id}"
                    )

        self._log.debug(
            f"{len(candidates)} Candidates found using tidal_album_id from items!"
        )
        self._log.debug("Searching for candidates using album + artist search")

        # Create query
        query = []
        if album:
            query.append(album)
        if artist:
            query.append(artist)

        candidates = candidates + self._search_album(" ".join(query))

        return candidates

    def item_candidates(self, item, artist, album):
        """Returns TIDAL metadata candidates for a specific item"""
        if not self._load_session():
            self._log.info(
                "Skipping item_candidates because we have no session! Please login."
            )
            return []

        self._log.debug(f"Searching TIDAL for {item}!")

        if item.title:
            return self._search_track(
                item.title,
                limit=self.config["metadata_search_limit"].as_number(),
            )
        elif item.album:
            return self._search_track(
                item.album,
                limit=self.config["metadata_search_limit"].as_number(),
            )
        elif item.artist:
            return self._search_track(
                item.artist,
                limit=self.config["metadata_search_limit"].as_number(),
            )

        return []

    def _album_to_albuminfo(self, album):
        """Converts a TIDAL album to a beets AlbumInfo

        :param album: An album obtained from the TIDAL API
        :type album: tidalapi.media.Album
        :return: A beets AlbumInfo type created with the provided data
        :rtype: beets.autotag.hooks.AlbumInfo
        """
        tracks = []

        # Process tracks
        # Not using sparse albums as we already have the album
        # so it's not using up any additional API calls.
        for track in album.tracks(sparse_album=False):
            tracks.append(self._track_to_trackinfo(track, album))

        albuminfo = AlbumInfo(
            album=album.name,
            album_id=album.id,
            tidal_album_id=album.id,
            artist=album.artist.name,
            artist_id=album.artist.id,
            tidal_artist_id=album.artist.id,
            va=len(album.artists) == 1
            and album.artist.name.lower() == "various artists",
            mediums=album.num_volumes,
            data_source=self.data_source,
            data_url=album.share_url,
            tracks=tracks,
            barcode=album.universal_product_number,
            albumtype=album.type,
        )

        # Add release date if we have one
        if album.release_date:
            albuminfo.year = album.release_date.year
            albuminfo.month = album.release_date.month
            albuminfo.day = album.release_date.day

        return albuminfo

    def _track_to_trackinfo(self, track, album=None):
        """Converts a TIDAL track to a beets TrackInfo

        :param track: A track obtained from the TIDAL API
        :type track: tidalapi.media.Track
        :param album: A tidalapi album to fill in optional track info with, defaults to None
        :type album: tidalapi.media.Album, optional
        :return: A beets TrackInfo created with the provided data
        :rtype: beets.autotag.hooks.TrackInfo
        """
        trackinfo = TrackInfo(
            title=track.name,
            track_id=track.id,
            tidal_track_id=track.id,
            artist=track.artist.name,
            artist_id=track.artist.id,
            album=track.album.name,
            length=track.duration,
            medium=track.volume_num,
            medium_index=track.track_num,
            index=track.track_num,
            data_source=self.data_source,
            data_url=track.share_url,
            isrc=track.isrc,
        )

        # If we're given an album, add it's data to the track.
        # Tidal does NOT return a lot of info on searches to save on bandwidth.
        if album:
            trackinfo.medium_total = album.num_tracks

        # if self.config["lyrics"]:
        #    trackinfo.lyrics = self._get_lyrics(track)

        return trackinfo

    def _search_track(self, query, limit=10, offset=0):
        """Searches TIDAL for tracks matching the query

        :param query: The search string to use
        :type query: str
        :param limit: Maximum number of items to return, defaults to 10
        :type limit: int, optional
        :param offset: Offset the items to retrieve, defaults to 0
        :type offset: int, optional
        :return: A list of tidalapi Tracks
        :rtype: list
        """
        self._log.debug(f"_search_track query {query}")
        results = self._tidal_search(query, [tidalapi.Track], limit, offset)

        candidates = []

        # top_hit is the most relevant to our query, add that first.
        if results["top_hit"]:
            candidates.append(results["top_hit"])

        for result in results["tracks"]:
            # Don't add top_hit twice
            if result.id == results["top_hit"].id:
                continue

            candidates.append(result)

        self._log.debug(f"_search_track found {len(candidates)} results")
        return candidates

    def _search_album(self, query, limit=10, offset=0):
        """Searches TIDAL for albums matching the query

        :param query: The search string to use
        :type query: str
        :param limit: Maximum number of items to return, defaults to 10
        :type limit: int, optional
        :param offset: Offset the items to retrieve, defaults to 0
        :type offset: int, optional
        :return: A list of tidalapi albums
        :rtype: list
        """
        self._log.debug(f"_search_album query {query}")
        results = self._tidal_search(query, [tidalapi.Album], limit, offset)
        candidates = []

        # top_hit is the most relevant to our query, add that first.
        if results["top_hit"]:
            candidates = [self._album_to_albuminfo(results["top_hit"])]

        for result in results["albums"]:
            # Don't add top_hit twice
            if result.id == results["top_hit"].id:
                continue

            candidates.append(self._album_to_albuminfo(result))

        self._log.debug(f"_search_album found {len(candidates)} results")
        return candidates

    @cachetools.cached(
        cache=cachetools.LFUCache(maxsize=4096),
        key=lambda self, query, *args, **kwargs: query,
        info=True,
    )
    @backoff.on_exception(
        backoff.expo,
        tidalapi.exceptions.TooManyRequests,
        max_tries=rate_limit_retries,
        on_backoff=backoff_handler,
        factor=2,
    )
    def _tidal_search(self, query, *args, **kwargs):
        """Simple wrapper for TIDAL search to check for session and to implement rate limiting

        :param query: The query to use for the search
        :type query: str
        :return: A dictionary of search results, including top hit
        :rtype: dict
        """
        # raise exceptions.TooManyRequests("test")
        if not self._load_session():
            self.log.debug(
                "Cannot perform search with no session... please login."
            )

            # We only use these three keys, even though the API returns more
            return {"albums": [], "tracks": [], "top_hit": None}

        return self.sess.search(query, *args, **kwargs)

    @cachetools.cached(
        cache=cachetools.LFUCache(maxsize=4096),
        key=lambda self, track: track.id,
        info=True,
    )
    # _get_lyrics has a much higher factor as it is much more rate limited by TIDAL than
    # the metadata API
    @backoff.on_exception(
        backoff.expo,
        tidalapi.exceptions.TooManyRequests,
        max_tries=rate_limit_retries,
        on_backoff=backoff_handler,
        base=5,
        factor=3,
    )
    def _get_lyrics(self, track):
        """Obtains lyrics from a TIDAL track

        :param track: The tidalapi track to obtain lyrics for
        :type track: tidalapi.media.Track
        :return: The lyrics if they are available, otherwise None.
        :rtype: str or None
        """
        if not self._load_session():
            self._log.debug(
                "Cannot grab lyrics with no session... please login."
            )
            return None

        self._log.debug(f"Grabbing lyrics for track {track.id}")

        # Grab lyrics
        try:
            lyrics = track.lyrics()
        except tidalapi.exceptions.MetadataNotAvailable:
            self._log.info(f"Lyrics not available for track {track.id}")
            return None

        # Return either synced lyrics or unsynced depending on config and availability
        if self.config["synced_lyrics"]:
            if lyrics.subtitles:
                self._log.debug(
                    f"Synced lyrics are available for track {track.id}"
                )
                return lyrics.subtitles
            else:
                self._log.info(
                    f"Synced lyrics not available for track {track.id}, returning unsynced lyrics"
                )
                return lyrics.text
        else:
            return lyrics.text

    def _search_lyrics(self, item, limit=10):
        """Searches for lyrics using a non-TIDAL metadata source

        :param item: The library item to search lyrics for
        :type item: beets.library.Item
        :param limit: Number of tidalapi tracks to attempt to match to library item with, defaults to 10
        :type limit: int, optional
        :return: The lyrics if they are available, otherwise nothing.
        :rtype: str or None
        """
        if not self._load_session():
            self._log.debug(
                "Cannot grab lyrics with no session... please login."
            )
            return None

        self._log.debug(
            f"Searching for lyrics from non-TIDAL metadata for {item.title}"
        )

        query = []
        tracks = []

        # Search using title
        if item.title:
            query.append(item.title)
            results = self._search_track(
                " ".join(query),
                limit=self.config["metadata_search_limit"].as_number(),
            )
            trackids = [x.id for x in results]
            tracks+=results

        # Search using title + artist
        if item.artist:
            query.append(item.artist)
            results = self._search_track(
                " ".join(query),
                limit=self.config["metadata_search_limit"].as_number(),
            )
            trackids += [x.id for x in results]
            tracks+=results

        # Search using title + artist + album
        if item.album:
            query.append(item.album)
            trackids += [x.id for x in tracks]
            results = self._search_track(
                " ".join(query),
                limit=self.config["metadata_search_limit"].as_number(),
            )
            trackids += [x.id for x in results]
            tracks+=results

        # Reverse list so the more specific result is first
        tracks = reversed(tracks)

        # Remove duplicates
        trackids = []
        newtracks = []
        for track in tracks:
            if track.id in trackids:
                self._log.debug(f"Removing duplicate track {track.id}")
                continue
            else:
                trackids.append(track.id)
                newtracks.append(track)

        tracks = newtracks

        # Fetch lyrics for tracks
        lyrics = []
        for track in tracks:
            lyric = self._get_lyrics(track)
            if lyric:
                # Only add to list if we actually have lyrics
                lyrics.append(lyric)

        if not tracks or not lyrics:
            self._log.info(f"No results found for {item.title}")
            return None

        # Pick best one, aka the first one with lyrics
        return lyrics[0]

    def _process_item(self, item):
        """Processes an item from the import stage

        This is used to simplify the stage loop.

        :param item: The library item to process
        :type item: beets.library.Item
        """

        # Fetch lyrics if enabled
        if self.config["lyrics"]:
            # Don't overwrite lyrics
            if not self.config["overwrite_lyrics"] and item.lyrics:
                self._log.info(
                    "Not fetching lyrics because item already has them"
                )
                return

            self._log.debug("Fetching lyrics during import... this may fail")
            # Use tidal_track_id if defined, aka the metadata came from us
            if item.get("tidal_track_id", None):
                self._log.debug(
                    f"Using tidal_track_id of {item.tidal_track_id} to fetch lyrics!"
                )
                try:
                    track = self.sess.track(item.tidal_track_id)
                except tidalapi.exceptions.ObjectNotFound:
                    self._log.warn(
                        "tidal_track_id is defined but the API returned not found"
                    )
                    return

                item.lyrics = self._get_lyrics(track)
            else:
                self._log.debug("tidal_track_id is undefined... searching")
                item.lyrics = self._search_lyrics(
                    item, limit=self.config["lyrics_search_limit"].as_number()
                )

            # Write out item if global write is enabled
            if ui.should_write():
                self._log.debug("Global write is enabled... writing lyrics")
                item.try_write()

            # Write out lyrics to sidecar file if enabled
            if self.config["write_sidecar"] and item.lyrics:
                self._log.debug(
                    "write_sidecar is enabled and we have lyrics... writing sidecar files"
                )

                # Do tons of os.path operations to get the sidecar path
                filepath, filename = os.path.split(syspath(item.path))
                basename, ext = os.path.splitext(filename)
                sidecar_file = f"{basename}.lrc"
                sidecar_path = os.path.join(filepath, sidecar_file)

                self._log.debug(f"Saving lyrics to sidecar file {sidecar_path}")

                # Save lyrics
                with open(sidecar_path, "w") as file:
                    file.write(item.lyrics)

            item.store()

    def stage(self, session: ImportSession, task: ImportTask):
        self._log.debug("Running import stage for TidalPlugin!")
        if not self.config["auto"]:
            self._log.debug("Not processing further due to auto being False")
            return

        if not self._load_session():
            self._log.info(
                "Skipping import stage because we have no session! Please login."
            )
            return

        for item in task.imported_items():
            self._process_item(item)

            self._log.debug(
                f"_get_lyrics cache: {self._get_lyrics.cache_info()}"
            )
            self._log.debug(
                f"_tidal_search cache: {self._tidal_search.cache_info()}"
            )
