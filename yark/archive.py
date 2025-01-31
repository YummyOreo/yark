"""Archive management with metadata/video downloading core"""

from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import time
from yt_dlp import YoutubeDL, DownloadError  # type: ignore
from colorama import Style, Fore
import sys
from .reporter import Reporter
from .errors import ArchiveNotFoundException, _err_msg, VideoNotFoundException
from .video import Video, Element, CommentAuthor
from typing import Optional
from .config import Config
from .converter import Converter

ARCHIVE_COMPAT = 4
"""
Version of Yark archives which this script is capable of properly parsing

- Version 1 was the initial format and had all the basic information you can see in the viewer now
- Version 2 introduced livestreams and shorts into the mix, as well as making the channel id into a general url
- Version 3 was a minor change to introduce a deleted tag so we have full reporting capability
- Version 4 introduced comments and moved `thumbnails/` to `images/` # TODO: more for 1.3

Some of these breaking versions are large changes and some are relatively small.
We don't check if a value exists or not in the archive format out of precedent
and we don't have optionally-present values, meaning that any new tags are a
breaking change to the format. The only downside to this is that the migrator
gets a line or two of extra code every breaking change. This is much better than
having way more complexity in the archiver decoding system itself.
"""


class VideoLogger:
    @staticmethod
    def downloading(d):
        """Progress hook for video downloading"""
        # Get video's id
        id = d["info_dict"]["id"]

        # Downloading percent
        if d["status"] == "downloading":
            percent = d["_percent_str"].strip()
            print(
                Style.DIM + f"  • Downloading {id}, at {percent}.." + Style.NORMAL,
                end="\r",
            )

        # Finished a video's download
        elif d["status"] == "finished":
            print(
                Style.DIM
                + f"  • Downloaded {id}                               "
                + Style.NORMAL
            )

    def debug(self, msg):
        """Debug log messages, ignored"""
        pass

    def info(self, msg):
        """Info log messages ignored"""
        pass

    def warning(self, msg):
        """Warning log messages ignored"""
        pass

    def error(self, msg):
        """Error log messages"""
        pass


class Archive:
    path: Path
    version: int
    url: str
    videos: list[Video]
    livestreams: list[Video]
    shorts: list[Video]
    comment_authors: dict[str, CommentAuthor]
    reporter: Reporter

    @staticmethod
    def new(path: Path, url: str) -> Archive:
        """Creates a new archive"""
        # Details
        print("Creating new archive..")
        archive = Archive()
        archive.path = Path(path)
        archive.version = ARCHIVE_COMPAT
        archive.url = url
        archive.videos = []
        archive.livestreams = []
        archive.shorts = []
        archive.comment_authors = {}
        archive.reporter = Reporter(archive)

        # Commit and return
        archive.commit()
        return archive

    @staticmethod
    def load(path: Path) -> Archive:
        """Loads existing archive from path"""
        # Check existence
        path = Path(path)
        archive_name = path.name
        print(f"Loading {archive_name} archive..")
        if not path.exists():
            raise ArchiveNotFoundException("Archive doesn't exist")

        # Load config
        encoded = json.load(open(path / "yark.json", "r"))

        # Check version before fully decoding and exit if wrong
        archive_version = encoded["version"]
        if archive_version != ARCHIVE_COMPAT:
            encoded = _migrate_archive(
                archive_version, ARCHIVE_COMPAT, encoded, path, archive_name
            )

        # Decode and return
        return Archive._from_dict(encoded, path)

    def metadata(self, config: Config):
        """Queries YouTube for all channel/playlist metadata to refresh known videos"""
        # Construct downloader
        print("Downloading metadata..")
        settings = self._md_settings(config)

        # Get response and snip it
        with YoutubeDL(settings) as ydl:
            for i in range(3):
                try:
                    res = ydl.extract_info(self.url, download=False)
                    break
                except Exception as exception:
                    # Report error
                    retrying = i != 2
                    _err_dl("metadata", exception, retrying)

                    # Print retrying message
                    if retrying:
                        print(
                            Style.DIM
                            + f"  • Retrying metadata download.."
                            + Style.RESET_ALL
                        )

        # Uncomment for saving big dumps for testing
        # with open("demo/dump.json", "w+") as file:
        #     json.dump(res, file)

        # Uncomment for loading big dumps for testing
        # res = json.load(open("demo/dump.json", "r"))

        # Make buckets to normalize different types of videos
        videos = []
        livestreams = []
        shorts = []

        # Videos only (basic channel or playlist)
        if "entries" not in res["entries"][0]:
            videos = res["entries"]

        # Videos and at least one other (livestream/shorts)
        else:
            for entry in res["entries"]:
                # Find the kind of category this is; youtube formats these as 3 playlists
                kind = entry["title"].split(" - ")[-1].lower()

                # Plain videos
                if kind == "videos":
                    videos = entry["entries"]

                # Livestreams
                elif kind == "live":
                    livestreams = entry["entries"]

                # Shorts
                elif kind == "shorts":
                    shorts = entry["entries"]

                # Unknown 4th kind; youtube might've updated
                else:
                    _err_msg(f"Unknown video kind '{kind}' found", True)

        # Parse metadata
        self._parse_metadata("video", config, videos, self.videos)
        self._parse_metadata("livestream", config, livestreams, self.livestreams)
        self._parse_metadata("shorts", config, shorts, self.shorts)

        # Go through each and report deleted
        self._report_deleted(self.videos)
        self._report_deleted(self.livestreams)
        self._report_deleted(self.shorts)

    def download(self, config: Config):
        """Downloads all videos which haven't already been downloaded"""
        # Prepare; clean out old part files and get settings
        self._clean_parts()
        settings = self._dl_settings(config)

        # Retry downloading 5 times in total for all videos
        anything_downloaded = True
        for i in range(5):
            # Try to curate a list and download videos on it
            try:
                # Curate list of non-downloaded videos
                not_downloaded = self._curate(config)

                # Stop if there's nothing to download
                if len(not_downloaded) == 0:
                    anything_downloaded = False
                    break

                # Print curated if this is the first time
                if i == 0:
                    _log_download_count(len(not_downloaded))

                # Launch core to download all curated videos
                self._dl_launch(settings, not_downloaded)

                # Stop if we've got them all
                break

            # Report error and retry/stop
            except Exception as exception:
                # Get around carriage return
                if i == 0:
                    print()

                # Report error
                _err_dl("videos", exception, i != 4)

        # End by converting any downloaded but unsupported video file formats
        if anything_downloaded:
            converter = Converter(self.path / "videos")
            converter.run()

    def _md_settings(self, config: Config) -> dict:
        """Generates customized yt-dlp settings for metadata from `config` passed in"""
        # Always present
        settings = {
            # Centralized logging system; makes output fully quiet
            "logger": VideoLogger(),
            # Skip downloading pending livestreams (#60 <https://github.com/Owez/yark/issues/60>)
            "ignore_no_formats_error": True,
            # Fetch comments from videos
            "getcomments": config.comments,
        }

        # Custom yt-dlp proxy
        if config.proxy is not None:
            settings["proxy"] = config.proxy

        # Return
        return settings

    def _dl_settings(self, config: Config) -> dict:
        """Generates customized yt-dlp settings from `config` passed in"""
        # Always present
        settings = {
            # Set the output path
            "outtmpl": f"{self.path}/videos/%(id)s.%(ext)s",
            # Centralized logger hook for ignoring all stdout
            "logger": VideoLogger(),
            # Logger hook for download progress
            "progress_hooks": [VideoLogger.downloading],
        }

        # Custom yt-dlp format
        if config.format is not None:
            settings["format"] = config.format

        # Custom yt-dlp proxy
        if config.proxy is not None:
            settings["proxy"] = config.proxy

        # Return
        return settings

    def _dl_launch(self, settings: dict, not_downloaded: list[Video]):
        """Downloads all `not_downloaded` videos passed into it whilst automatically handling privated videos, this is the core of the downloader"""
        # Continuously try to download after private/deleted videos are found
        # This block gives the downloader all the curated videos and skips/reports deleted videos by filtering their exceptions
        while True:
            # Download from curated list then exit the optimistic loop
            try:
                urls = [video.url() for video in not_downloaded]
                with YoutubeDL(settings) as ydl:
                    ydl.download(urls)
                break

            # Special handling for private/deleted videos which are archived, if not we raise again
            except DownloadError as exception:
                new_not_downloaded = self._dl_exception_handle(
                    not_downloaded, exception
                )
                if new_not_downloaded is not None:
                    not_downloaded = new_not_downloaded

    def _dl_exception_handle(
        self, not_downloaded: list[Video], exception: DownloadError
    ) -> Optional[list[Video]]:
        """Handle for failed downloads if there's a special private/deleted video"""
        # Set new list for not downloaded to return later
        new_not_downloaded = None

        # Video is privated or deleted
        if (
            "Private video" in exception.msg
            or "This video has been removed by the uploader" in exception.msg
        ):
            # Skip video from curated and get it as a return
            new_not_downloaded, video = _skip_video(not_downloaded, "deleted")

            # If this is a new occurrence then set it & report
            # This will only happen if its deleted after getting metadata, like in a dry run
            if video.deleted.current() == False:
                self.reporter.deleted.append(video)
                video.deleted.update(None, True)

        # User hasn't got ffmpeg installed and youtube hasn't got format 22
        # NOTE: see #55 <https://github.com/Owez/yark/issues/55> to learn more
        # NOTE: sadly yt-dlp doesn't let us access yt_dlp.utils.ContentTooShortError so we check msg
        elif " bytes, expected " in exception.msg:
            # Skip video from curated
            new_not_downloaded, _ = _skip_video(
                not_downloaded,
                "no format found; please download ffmpeg!",
                True,
            )

        # Nevermind, normal exception
        else:
            raise exception

        # Return
        return new_not_downloaded

    def search(self, id: str):
        """Searches archive for a video with the corresponding `id` and returns"""
        # Search
        for video in self.videos:
            if video.id == id:
                return video

        # Raise exception if it's not found
        raise VideoNotFoundException(f"Couldn't find {id} inside archive")

    def _curate(self, config: Config) -> list[Video]:
        """Curate videos which aren't downloaded and return their urls"""

        def curate_list(videos: list[Video], maximum: Optional[int]) -> list[Video]:
            """Curates the videos inside of the provided `videos` list to it's local maximum"""
            # Cut available videos to maximum if present for deterministic getting
            if maximum is not None:
                # Fix the maximum to the length so we don't try to get more than there is
                fixed_maximum = min(max(len(videos) - 1, 0), maximum)

                # Set the available videos to this fixed maximum
                new_videos = []
                for ind in range(fixed_maximum):
                    new_videos.append(videos[ind])
                videos = new_videos

            # Find undownloaded videos in available list
            not_downloaded = []
            for video in videos:
                if not video.downloaded():
                    not_downloaded.append(video)

            # Return
            return not_downloaded

        # Curate
        not_downloaded = []
        not_downloaded.extend(curate_list(self.videos, config.max_videos))
        not_downloaded.extend(curate_list(self.livestreams, config.max_livestreams))
        not_downloaded.extend(curate_list(self.shorts, config.max_shorts))

        # Return
        return not_downloaded

    def commit(self):
        """Commits (saves) archive to path; do this once you've finished all of your transactions"""
        # Save backup
        self._backup()

        # Directories
        print(f"Committing {self} to file..")
        paths = [self.path, self.path / "images", self.path / "videos"]
        for path in paths:
            if not path.exists():
                path.mkdir()

        # Config
        with open(self.path / "yark.json", "w+") as file:
            json.dump(self._to_dict(), file)

    def _parse_metadata(
        self, kind: str, config: Config, entries: list[dict], videos: list[Video]
    ):
        """Parses metadata for a category of video into it's `videos` bucket"""
        # Parse each video
        print(f"Parsing {kind} metadata..")
        for entry in entries:
            self._parse_metadata_video(config, entry, videos)

        # Sort videos by newest
        videos.sort(reverse=True)

    def _parse_metadata_video(self, config: Config, entry: dict, videos: list[Video]):
        """Parses metadata for one video, creating it or updating it depending on the `videos` already in the bucket"""
        # Skip video if there's no formats available; happens with upcoming videos/livestreams
        if "formats" not in entry or len(entry["formats"]) == 0:
            return

        # Updated intra-loop marker
        updated = False

        # Update video if it exists
        for video in videos:
            if video.id == entry["id"]:
                video.update(config, entry)
                updated = True
                break

        # Add new video if not
        if not updated:
            video = Video.new(config, entry, self)
            videos.append(video)
            self.reporter.added.append(video)

    def _report_deleted(self, videos: list):
        """Goes through a video category to report & save those which where not marked in the metadata as deleted if they're not already known to be deleted"""
        for video in videos:
            if video.deleted.current() == False and not video.known_not_deleted:
                self.reporter.deleted.append(video)
                video.deleted.update(None, True)

    def _clean_parts(self):
        """Cleans old temporary `.part` files which where stopped during download if present"""
        # Make a bucket for found files
        deletion_bucket: list[Path] = []

        # Scan through and find part files
        # NOTE: can this be improved with a set and 2x path.glob()?
        videos = self.path / "videos"
        for file in videos.iterdir():
            if file.suffix == ".part" or file.suffix == ".ytdl":
                deletion_bucket.append(file)

        # Print and delete if there are part files present
        if len(deletion_bucket) != 0:
            print("Cleaning out previous temporary files..")
            for file in deletion_bucket:
                file.unlink()

    def _backup(self):
        """Creates a backup of the existing `yark.json` file in path as `yark.bak` with added comments"""
        # Get current archive path
        ARCHIVE_PATH = self.path / "yark.json"

        # Skip backing up if the archive doesn't exist
        if not ARCHIVE_PATH.exists():
            return

        # Open original archive to copy
        with open(self.path / "yark.json", "r") as file_archive:
            # Add comment information to backup file
            save = f"// Backup of a Yark archive, dated {datetime.utcnow().isoformat()}\n// Remove these comments and rename to 'yark.json' to restore\n{file_archive.read()}"

            # Save new information into a new backup
            with open(self.path / "yark.bak", "w+") as file_backup:
                file_backup.write(save)

    @staticmethod
    def _from_dict(encoded: dict, path: Path) -> Archive:
        """Decodes archive which is being loaded back up"""
        # Initiate archive
        archive = Archive()

        # Decode head & body style comment authors; needed above video decoding for comments
        archive.comment_authors = {}
        for id in encoded["comment_authors"].keys():
            archive.comment_authors[id] = CommentAuthor._from_dict_head(
                archive, id, encoded["comment_authors"][id]
            )

        # Basics
        archive.path = path
        archive.version = encoded["version"]
        archive.url = encoded["url"]
        archive.reporter = Reporter(archive)
        archive.videos = [
            Video._from_dict(video, archive) for video in encoded["videos"]
        ]
        archive.livestreams = [
            Video._from_dict(video, archive) for video in encoded["livestreams"]
        ]
        archive.shorts = [
            Video._from_dict(video, archive) for video in encoded["shorts"]
        ]
        archive.comment_authors = {}

        # Return
        return archive

    def _to_dict(self) -> dict:
        """Converts archive data to a dictionary to commit"""
        # Encode comment authors
        comment_authors = {}
        for id in self.comment_authors.keys():
            comment_authors[id] = self.comment_authors[id]._to_dict_head()

        # Basics
        payload = {
            "version": self.version,
            "url": self.url,
            "videos": [video._to_dict() for video in self.videos],
            "livestreams": [video._to_dict() for video in self.livestreams],
            "shorts": [video._to_dict() for video in self.shorts],
            "comment_authors": comment_authors,
        }

        # Return
        return payload

    def __repr__(self) -> str:
        return self.path.name


def _log_download_count(count: int):
    """Tells user that `count` number of videos have been downloaded"""
    fmt_num = "a new video" if count == 1 else f"{count} new videos"
    print(f"Downloading {fmt_num}..")


def _skip_video(
    videos: list[Video],
    reason: str,
    warning: bool = False,
) -> tuple[list[Video], Video]:
    """Skips first undownloaded video in `videos`, make sure there's at least one to skip otherwise an exception will be thrown"""
    # Find fist undownloaded video
    for ind, video in enumerate(videos):
        if not video.downloaded():
            # Tell the user we're skipping over it
            if warning:
                print(
                    Fore.YELLOW + f"  • Skipping {video.id} ({reason})" + Fore.RESET,
                    file=sys.stderr,
                )
            else:
                print(
                    Style.DIM + f"  • Skipping {video.id} ({reason})" + Style.NORMAL,
                )

            # Set videos to skip over this one
            videos = videos[ind + 1 :]

            # Return the corrected list and the video found
            return videos, video

    # Shouldn't happen, see docs
    raise Exception(
        "We expected to skip a video and return it but nothing to skip was found"
    )


def _migrate_archive(
    current_version: int,
    expected_version: int,
    encoded: dict,
    path: Path,
    archive_name: str,
) -> dict:
    """Automatically migrates an archive from one version to another by bootstrapping"""

    def migrate_step(cur: int, encoded: dict) -> dict:
        """Step in recursion to migrate from one to another, contains migration logic"""
        # Stop because we've reached the desired version
        if cur == expected_version:
            return encoded

        # From version 1 to version 2
        elif cur == 1:
            # Target id to url
            encoded["url"] = "https://www.youtube.com/channel/" + encoded["id"]
            del encoded["id"]
            print(
                Fore.YELLOW
                + "Please make sure "
                + encoded["url"]
                + " is the correct url"
                + Fore.RESET
            )

            # Empty livestreams/shorts lists
            encoded["livestreams"] = []
            encoded["shorts"] = []

        # From version 2 to version 3
        elif cur == 2:
            # Add deleted status to every video/livestream/short
            # NOTE: none is fine for new elements, just a slight bodge
            for video in encoded["videos"]:
                video["deleted"] = Element.new(Video._new_empty(), False)._to_dict()
            for video in encoded["livestreams"]:
                video["deleted"] = Element.new(Video._new_empty(), False)._to_dict()
            for video in encoded["shorts"]:
                video["deleted"] = Element.new(Video._new_empty(), False)._to_dict()

        # From version 3 to version 4
        elif cur == 3:
            # Add empty comment author store
            encoded["comment_authors"] = {}

            # Add blank comment section to each video
            for video in encoded["videos"]:
                video["comments"] = {}
            for video in encoded["livestreams"]:
                video["comments"] = {}
            for video in encoded["shorts"]:
                video["comments"] = {}

            # Rename thumbnails directory to images
            try:
                thumbnails = path / "thumbnails"
                thumbnails.rename(path / "images")
            except:
                _err_msg(
                    f"Couldn't rename {archive_name}/thumbnails directory to {archive_name}/images, please manually rename to continue!"
                )
                sys.exit(1)

            # Convert unsupported formats, because of #75 <https://github.com/Owez/yark/issues/75>
            converter = Converter(path / "videos")
            converter.run()

        # Unknown version
        else:
            _err_msg(f"Unknown archive version v{cur} found during migration", True)
            sys.exit(1)

        # Increment version and run again until version has been reached
        cur += 1
        encoded["version"] = cur
        return migrate_step(cur, encoded)

    # Inform user of the backup process
    print(
        Fore.YELLOW
        + f"Automatically migrating archive from v{current_version} to v{expected_version}, a backup has been made at {archive_name}/yark.bak"
        + Fore.RESET
    )

    # Start recursion step
    return migrate_step(current_version, encoded)


def _err_dl(name: str, exception: DownloadError, retrying: bool):
    """Prints errors to stdout depending on what kind of download error occurred"""
    # Default message
    msg = f"Unknown error whilst downloading {name}, details below:\n{exception}"

    # Types of errors
    ERRORS = [
        "<urlopen error [Errno 8] nodename nor servname provided, or not known>",
        "500",
        "Got error: The read operation timed out",
        "No such file or directory",
        "HTTP Error 404: Not Found",
        "<urlopen error timed out>",
    ]

    # Download errors
    if type(exception) == DownloadError:
        # Server connection
        if ERRORS[0] in exception.msg:
            msg = "Issue connecting with YouTube's servers"

        # Server fault
        elif ERRORS[1] in exception.msg:
            msg = "Fault with YouTube's servers"

        # Timeout
        elif ERRORS[2] in exception.msg:
            msg = "Timed out trying to download video"

        # Video deleted whilst downloading
        elif ERRORS[3] in exception.msg:
            msg = "Video deleted whilst downloading"

        # Target not found, might need to retry with alternative route
        elif ERRORS[4] in exception.msg:
            msg = "Couldn't find target by it's id"

        # Random timeout; not sure if its user-end or youtube-end
        elif ERRORS[5] in exception.msg:
            msg = "Timed out trying to reach YouTube"

    # Print error
    suffix = ", retrying in a few seconds.." if retrying else ""
    print(
        Fore.YELLOW + "  • " + msg + suffix.ljust(40) + Fore.RESET,
        file=sys.stderr,
    )

    # Wait if retrying, exit if failed
    if retrying:
        time.sleep(5)
    else:
        _err_msg(f"  • Sorry, failed to download {name}", True)
        sys.exit(1)
