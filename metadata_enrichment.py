import os
import time
import json
import socket
import urllib.request
import urllib.parse
from pathlib import Path
from mutagen.flac import FLAC, Picture
import musicbrainzngs

musicbrainzngs.set_useragent("local-musiclib", "1.0.0", "https://github.com/local-musiclib")
socket.setdefaulttimeout(30)

LASTFM_API_KEY = "11ba6ce65ed7978c4a2f182273bf367f"
MAX_RETRIES = 3
_release_cache = {}


def mb_request(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except (musicbrainzngs.NetworkError, TimeoutError, OSError):
            if attempt == MAX_RETRIES - 1:
                raise musicbrainzngs.NetworkError(cause=Exception("Max retries exceeded"))
            print(f"         Network error, retrying ({attempt + 1}/{MAX_RETRIES})...")
            time.sleep(5 * (attempt + 1))


def format_date(date_str):
    if not date_str:
        return ""
    clean = date_str.strip()
    parts = clean.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return clean
    if len(parts) == 2 and len(parts[0]) == 4:
        return clean
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) == 4:
        return parts[0]
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return clean


def get_genres_from_artist(artist_id):
    try:
        time.sleep(1)
        result = mb_request(musicbrainzngs.get_artist_by_id, artist_id, includes=["tags"])
        tag_list = result.get("artist", {}).get("tag-list", [])
        if tag_list:
            tag_list.sort(key=lambda t: int(t.get("count", 0)), reverse=True)
            return [t["name"].title() for t in tag_list if int(t.get("count", 0)) > 0]
    except Exception:
        pass
    return []


def get_genres_from_lastfm(artist):
    if LASTFM_API_KEY == "YOUR_API_KEY_HERE":
        return []
    try:
        params = urllib.parse.urlencode({
            "method": "artist.getTopTags",
            "artist": artist,
            "api_key": LASTFM_API_KEY,
            "format": "json",
        })
        url = f"https://ws.audioscrobbler.com/2.0/?{params}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        tags = data.get("toptags", {}).get("tag", [])
        genres = [
            t["name"].title() for t in tags
            if int(t.get("count", 0)) >= 25
        ]
        return genres[:10]
    except Exception:
        return []


def get_release_data(artist, album):
    cache_key = (artist.lower(), album.lower())
    if cache_key in _release_cache:
        return _release_cache[cache_key]

    time.sleep(1)
    results = mb_request(musicbrainzngs.search_releases, artist=artist, release=album, limit=10)
    release_list = results.get("release-list", [])

    if not release_list:
        _release_cache[cache_key] = None
        return None

    album_lower = album.lower().strip()
    best = None
    for r in release_list:
        if r.get("title", "").lower().strip() == album_lower:
            best = r
            break
    if not best:
        for r in release_list:
            if album_lower in r.get("title", "").lower().strip():
                best = r
                break
    if not best:
        best = release_list[0]

    release_id = best["id"]

    time.sleep(1)
    release = mb_request(
        musicbrainzngs.get_release_by_id,
        release_id,
        includes=["artists", "recordings", "release-groups", "tags", "media"]
    )["release"]

    genres = []
    rg = release.get("release-group", {})
    tag_list = rg.get("tag-list", [])
    if tag_list:
        tag_list.sort(key=lambda t: int(t.get("count", 0)), reverse=True)
        genres = [t["name"].title() for t in tag_list if int(t.get("count", 0)) > 0]

    if not genres:
        tag_list = release.get("tag-list", [])
        if tag_list:
            tag_list.sort(key=lambda t: int(t.get("count", 0)), reverse=True)
            genres = [t["name"].title() for t in tag_list if int(t.get("count", 0)) > 0]

    if not genres:
        artist_credits = release.get("artist-credit", [])
        for credit in artist_credits:
            if isinstance(credit, dict):
                artist_id = credit.get("artist", {}).get("id")
                if artist_id:
                    genres = get_genres_from_artist(artist_id)
                    if genres:
                        break

    if not genres:
        artist_credits = release.get("artist-credit", [])
        for credit in artist_credits:
            if isinstance(credit, dict):
                artist_name = credit.get("artist", {}).get("name")
                if artist_name:
                    genres = get_genres_from_lastfm(artist_name)
                    if genres:
                        break

    cover_data = None
    try:
        time.sleep(1)
        cover_data = mb_request(musicbrainzngs.get_image_front, release_id)
    except (musicbrainzngs.ResponseError, musicbrainzngs.NetworkError, TimeoutError, OSError):
        pass

    album_artist = None
    artist_credits = release.get("artist-credit", [])
    if artist_credits:
        main = [c for c in artist_credits if isinstance(c, dict)]
        if main:
            album_artist = main[0].get("artist", {}).get("name", "")

    data = {
        "release_id": release_id,
        "release": release,
        "genres": genres,
        "cover_data": cover_data,
        "album_artist": album_artist,
    }
    _release_cache[cache_key] = data
    return data


def vinyl_track_to_int(raw, side_offsets):
    """Convert a vinyl-style track number like 'A1', 'B3' to a sequential integer.

    side_offsets maps side letters to cumulative track counts from prior sides,
    e.g. {'A': 0, 'B': 5} means side A had 5 tracks so B1 -> 6.
    """
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    match_letter = ""
    rest = raw
    for ch in raw:
        if ch.isalpha():
            match_letter += ch.upper()
        else:
            break
    rest = raw[len(match_letter):]
    if match_letter and rest.isdigit():
        offset = side_offsets.get(match_letter, 0)
        return offset + int(rest)
    return None


def build_side_offsets(medium):
    """Build a mapping of side letter -> cumulative offset from prior sides."""
    sides = {}
    for track in medium.get("track-list", []):
        raw = track.get("number", "")
        prefix = ""
        for ch in raw:
            if ch.isalpha():
                prefix += ch.upper()
            else:
                break
        if prefix:
            sides.setdefault(prefix, 0)
            sides[prefix] += 1
    offsets = {}
    cumulative = 0
    for side in sorted(sides.keys()):
        offsets[side] = cumulative
        cumulative += sides[side]
    return offsets


def find_track_info(release, title):
    title_lower = title.lower().strip()
    medium_list = release.get("medium-list", [])
    prior_tracks = 0
    for medium in medium_list:
        track_list = medium.get("track-list", [])
        side_offsets = build_side_offsets(medium)
        for track in track_list:
            recording = track.get("recording", {})
            if recording.get("title", "").lower().strip() == title_lower:
                raw = track.get("number", "")
                position = track.get("position")

                if position is not None:
                    track_num = str(prior_tracks + int(position))
                else:
                    converted = vinyl_track_to_int(raw, side_offsets)
                    if converted is not None:
                        track_num = str(prior_tracks + converted)
                    else:
                        track_num = raw

                artists = []
                for credit in recording.get("artist-credit", []):
                    if isinstance(credit, dict):
                        artists.append(credit.get("artist", {}).get("name", ""))
                return track_num, artists
        prior_tracks += len(track_list)
    return None, []


def metadata_enrichment(force=False):
    ROOT_DIR = Path(r"D:\Lossless Music")

    flac_files = []
    for dirpath, _, filenames in os.walk(ROOT_DIR):
        for fname in filenames:
            if fname.lower().endswith(".flac"):
                flac_files.append(Path(dirpath) / fname)
    total = len(flac_files)

    if total == 0:
        print("No FLAC files found to enrich.")
        return

    if force:
        print(f"FORCE MODE: Re-processing all {total} files...\n")
    else:
        print(f"Enriching metadata for {total} files...\n")

    KEEP_TAGS = {"title", "artist", "album", "albumartist", "date", "genre", "tracknumber", "lyrics"}

    for i, item_path in enumerate(flac_files, 1):
        metadata = FLAC(item_path)

        title = metadata.get("TITLE", [None])[0]
        artist = metadata.get("ARTIST", [None])[0]
        album = metadata.get("ALBUM", [None])[0]

        if not all([title, artist, album]):
            print(f"[{i}/{total}] SKIP (missing tags): {item_path.name}")
            continue

        if not force:
            genre_vals = metadata.get("GENRE", [])
            date_vals = metadata.get("DATE", [])
            track_vals = metadata.get("TRACKNUMBER", [])
            artist_vals = metadata.get("ARTIST", [])
            aa_vals = metadata.get("ALBUMARTIST", [])

            has_extra_tags = any(
                k.lower() not in KEEP_TAGS for k in metadata.keys()
            )

            checks = {
                "cover": len(metadata.pictures) > 0,
                "genre": len(genre_vals) == 1 and bool(genre_vals[0]),
                "date": len(date_vals) == 1 and bool(date_vals[0]),
                "track": len(track_vals) == 1 and bool(track_vals[0]),
                "artist": len(artist_vals) == 1 and bool(artist_vals[0]),
                "albumartist": len(aa_vals) == 1 and bool(aa_vals[0]),
                "clean_tags": not has_extra_tags,
            }

            if all(checks.values()):
                print(f"[{i}/{total}] SKIP (already clean): {item_path.name}")
                continue

        print(f"[{i}/{total}] Looking up: {artist} - {album} - {title}")

        try:
            data = get_release_data(artist, album)
        except musicbrainzngs.NetworkError as e:
            print(f"         Network error after retries, skipping: {e}\n")
            continue

        if data is None:
            print(f"         No MusicBrainz match found, skipping.\n")
            continue

        release = data["release"]
        matched_title = release.get("title", "")

        if matched_title.lower().strip() != album.lower().strip():
            print(f"         MISMATCH: file has \"{album}\" but MusicBrainz returned \"{matched_title}\", skipping.\n")
            continue

        release_tracks = set()
        for medium in release.get("medium-list", []):
            for track in medium.get("track-list", []):
                rec_title = track.get("recording", {}).get("title", "")
                release_tracks.add(rec_title.lower().strip())

        if release_tracks and title.lower().strip() not in release_tracks:
            print(f"         CROSS-CHECK FAIL: \"{title}\" not found in release tracklist, skipping.\n")
            continue

        existing_track = metadata.get("TRACKNUMBER", [""])[0]
        existing_lyrics = metadata.get("LYRICS", [""])[0]


        for key in list(metadata.keys()):
            if key.lower() != "lyrics":
                del metadata[key]

        if existing_lyrics:
            metadata["LYRICS"] = [existing_lyrics]


        metadata["TITLE"] = [title]

        date = release.get("date", "")
        formatted_date = ""
        if date:
            formatted_date = format_date(date)
            metadata["DATE"] = [formatted_date]

        metadata["ALBUM"] = [release.get("title", album)]

        if data["album_artist"]:
            metadata["ALBUMARTIST"] = [data["album_artist"]]

        genre_str = ""
        if data["genres"]:
            genre_str = ", ".join(data["genres"])
            metadata["GENRE"] = [genre_str]

        track_num, track_artists = find_track_info(release, title)
        if track_num:
            metadata["TRACKNUMBER"] = [track_num]
        else:
            clean = existing_track.split("/")[0].split("\\")[0].strip()
            digits_only = "".join(c for c in clean if c.isdigit())
            if digits_only:
                metadata["TRACKNUMBER"] = [str(int(digits_only))]

        if track_artists:
            metadata["ARTIST"] = [", ".join(track_artists)]
        else:
            metadata["ARTIST"] = [artist]

        if data["cover_data"]:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = data["cover_data"]
            metadata.clear_pictures()
            metadata.add_picture(pic)

        metadata.save()

        changes = []
        if formatted_date: changes.append(f"date={formatted_date}")
        if genre_str: changes.append(f"genre={genre_str}")
        if track_num: changes.append(f"track={track_num}")
        if data["cover_data"]: changes.append("cover=yes")
        if track_artists: changes.append(f"artists={', '.join(track_artists)}")
        if data["album_artist"]: changes.append(f"albumartist={data['album_artist']}")
        print(f"         Updated: {', '.join(changes)}\n")

    print("Metadata enrichment complete.")


if __name__ == "__main__":
    metadata_enrichment()
