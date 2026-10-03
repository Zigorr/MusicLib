from mutagen.flac import FLAC
from pathlib import Path
from difflib import SequenceMatcher
from lrclib import LrcLibAPI
from lrclib.exceptions import APIError, NotFoundError, RateLimitError
import miniaudio
import numpy as np
import os
import re
import sys
import time

sys.stdout.reconfigure(errors="replace")
sys.stderr.reconfigure(errors="replace")

def sanitize_name(full_str: str, blacklist: str):
    clean = full_str
    for ch in blacklist:
        clean = clean.replace(ch, "")
    return clean

def remove_empty_folders(path):
    for dirpath, dirnames, _ in os.walk(path, topdown=False):
      for d in dirnames:
        full = os.path.join(dirpath, d)
        if not os.listdir(full):
          os.rmdir(full)


TITLE_MIN = 0.92
ARTIST_MIN = 0.85
DURATION_MAX_DELTA = 5
_FEAT_RE = re.compile(r"\b(?:featuring|feat|ft)\b", re.IGNORECASE)
_FEAT_GROUP_RE = re.compile(
    r"[([][^)\]]*\b(?:featuring|feat|ft)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_EXTRA_GROUP_RE = re.compile(
    r"[([][^)\]]*\b(?:remix|remaster(?:ed)?|version|edit|live|radio|extended|deluxe)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_VERSION_TAIL_RE = re.compile(
    r"(?:\s*[-–—:]\s*|\s+)(?:extended|single|album|radio|explicit|deluxe|clean|original|remaster(?:ed)?)\s+(?:version|mix|edit)\b.*$",
    re.IGNORECASE,
)
_TAG_LINE = re.compile(r"^\[[A-Za-z][^]:]*:[^\]]*\]$")
_TIME_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def _tag(metadata, key):
    values = metadata.get(key, [])
    if not values or not values[0]:
        return ""
    return str(values[0]).strip()


def normalize(text):
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def split_artists(artist):
    cleaned = _FEAT_GROUP_RE.sub(" ", artist)
    parts = _FEAT_RE.split(cleaned)
    names = []
    for part in parts:
        for piece in part.split(","):
            piece = piece.strip(" .")
            if piece:
                names.append(piece)
    return names


def artist_queries(artist, album_artist):
    first = ""
    parts = split_artists(artist) if artist else []
    if parts:
        first = parts[0]
    names = []
    for name in (album_artist, first, artist):
        name = (name or "").strip()
        if not name:
            continue
        if any(name.casefold() == existing.casefold() for existing in names):
            continue
        names.append(name)
    return names


def base_title(title):
    cleaned = _FEAT_GROUP_RE.sub(" ", title or "")
    cleaned = _EXTRA_GROUP_RE.sub(" ", cleaned)
    cleaned = _VERSION_TAIL_RE.sub(" ", cleaned)
    cleaned = _FEAT_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def title_queries(title):
    names = []
    for name in (title, base_title(title)):
        name = (name or "").strip()
        if not name:
            continue
        if any(name.casefold() == existing.casefold() for existing in names):
            continue
        names.append(name)
    return names


def title_score(file_title, candidate_title):
    file_base = base_title(file_title)
    candidate_base = base_title(candidate_title)
    return max(
        _ratio(file_title, candidate_title),
        _ratio(file_base, candidate_base),
        _ratio(file_base, candidate_title),
    )


def _ratio(left, right):
    left = normalize(left)
    right = normalize(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _names_overlap(left, right):
    left = normalize(left)
    right = normalize(right)
    if not left or not right:
        return False
    if left == right:
        return True
    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def artist_score(file_artist, candidate_artist):
    file_names = split_artists(file_artist)
    candidate_names = split_artists(candidate_artist)
    best = _ratio(file_artist, candidate_artist)
    for name in file_names:
        for other in candidate_names:
            if _names_overlap(name, other):
                return 1.0
            best = max(best, _ratio(name, other))
    return best


def parse_lrc_lines(text):
    parsed = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _TAG_LINE.match(line):
            continue
        match = _TIME_LINE.match(line)
        if not match:
            continue
        seconds = int(match.group(1)) * 60 + float(match.group(2))
        parsed.append((seconds, match.group(3).strip()))
    return parsed


def cue_times(text):
    return [seconds for seconds, lyric in parse_lrc_lines(text) if lyric]


def sheet_overruns(cues, length):
    """A sheet timed for a longer edit overruns this file. Chopping the tail
    leaves the remaining lines on the wrong part of the song."""
    if not cues or not length:
        return False
    past = [seconds for seconds in cues if seconds > length + 1.5]
    if cues[-1] > length + 8 and len(past) >= 2:
        return True
    return len(past) >= 3 and len(past) / len(cues) >= 0.10


def format_synced(cues, length):
    lines = []
    last_time = -1.0
    for seconds, lyric in cues:
        if not lyric:
            continue
        if seconds < -0.05:
            continue
        if seconds + 0.05 < last_time:
            continue
        if length and seconds > length + 1.5:
            continue
        minutes = int(seconds // 60)
        remain = seconds - minutes * 60
        lines.append(f"[{minutes:02d}:{remain:05.2f}] {lyric}")
        last_time = seconds
    if not lines:
        return None
    if length and length > 45 and last_time < length * 0.4:
        return None
    return "\n".join(lines) + "\n"


def shift_synced(text, offset, length):
    shifted = [(seconds + offset, lyric) for seconds, lyric in parse_lrc_lines(text)]
    return format_synced(shifted, length)


def onset_curve(path):
    """Vocal-onset strength at 20 Hz. None when the file cannot be decoded."""
    try:
        decoded = miniaudio.decode_file(str(path), nchannels=1, sample_rate=8000)
        samples = np.asarray(decoded.samples, dtype=np.float32)
    except (OSError, ValueError, RuntimeError):
        return None
    if samples.size < 8000:
        return None
    if np.max(np.abs(samples)) > 1.5:
        samples = samples / 32768.0
    sample_rate = 8000
    window = int(0.30 * sample_rate)
    hop = int(0.05 * sample_rate)
    taper = np.hanning(window)
    previous = None
    times = []
    flux = []
    for start in range(0, len(samples) - window, hop):
        spectrum = np.abs(np.fft.rfft(samples[start:start + window] * taper))
        if previous is None:
            previous = spectrum
            continue
        times.append(start / sample_rate)
        flux.append(np.maximum(spectrum - previous, 0).sum())
        previous = spectrum
    if len(flux) < 20:
        return None
    times = np.asarray(times)
    flux = np.asarray(flux, dtype=np.float64)
    flux = np.clip(flux / (np.percentile(flux, 95) + 1e-9), 0, 3)
    flux = np.convolve(flux, np.ones(3) / 3, mode="same")
    return times, flux


def _onset_score(cues, times, flux, offset, length):
    if len(cues) < 4:
        return None
    shifted = cues + offset
    kept = shifted[(shifted >= 0) & (shifted <= length)]
    if len(kept) < max(4, int(0.8 * len(cues))):
        return None
    index = np.clip(np.searchsorted(times, kept), 0, len(flux) - 1)
    return float(np.mean([np.max(flux[i:i + 7]) for i in index]))


def align_cues(cues, onset, length):
    """Return offset, score, and whether the published sheet can be synced.

    A constant offset is applied only when one shift within six seconds is a
    sharp, lone peak on the vocal onsets. A fuzzy bump is left as published.
    """
    cues = np.asarray(cues, dtype=np.float64)
    if onset is None or len(cues) < 4 or not length:
        return {"offset": 0.0, "score": None, "usable": True}
    times, flux = onset
    scores = {}
    for step in range(-24, 25):
        offset = round(step / 4, 2)
        scores[offset] = _onset_score(cues, times, flux, offset, length)
    present = {offset: score for offset, score in scores.items() if score is not None}
    at_zero = present.get(0.0)
    if not present or not at_zero:
        return {"offset": 0.0, "score": at_zero, "usable": at_zero is not None}
    best_offset = max(present, key=present.get)
    best = present[best_offset]
    if abs(best_offset) < 0.26:
        return {"offset": 0.0, "score": at_zero, "usable": True}
    lift = best / at_zero - 1
    shoulders = [
        present.get(round(best_offset - 0.5, 2)),
        present.get(round(best_offset + 0.5, 2)),
    ]
    shoulders = [score for score in shoulders if score]
    sharp = (best / max(shoulders) - 1) if shoulders else 0
    rivals = [score for offset, score in present.items() if abs(offset - best_offset) >= 1.5]
    unique = (best / max(rivals) - 1) if rivals else 1
    # A few seconds of constant error is common. Only move the sheet when that
    # correction is a sharp, lone peak. Anything fuzzier stays as published.
    if lift >= 0.10 and sharp >= 0.10 and unique >= 0.03:
        return {"offset": best_offset, "score": best, "usable": True}
    return {"offset": 0.0, "score": at_zero, "usable": True}


def clean_synced_lyrics(text, length):
    """Drop blank cues, metadata, backwards jumps, and cues past the end of the file.

    Blank cues are what Feishin highlights as missing lyrics. A timestamp that jumps
    back to the start, or sits after the audio ends, pulls the highlight off the vocal.
    """
    return format_synced(parse_lrc_lines(text), length)


def lyric_body(record, length):
    if record.instrumental:
        return "[Instrumental]", False, True
    synced = (record.synced_lyrics or "").strip()
    plain = (record.plain_lyrics or "").strip()
    if synced and not sheet_overruns(cue_times(synced), length):
        cleaned = clean_synced_lyrics(synced, length)
        if cleaned:
            return cleaned, True, False
    if plain:
        return plain, False, False
    return None


def score_record(record, title, artist, album, duration, length, check_duration=True):
    if record is None or record.duration is None:
        return None
    body = lyric_body(record, length)
    if body is None:
        return None
    delta = abs(int(record.duration) - duration)
    title_ratio = title_score(title, record.track_name or "")
    artist_ratio = artist_score(artist, record.artist_name or "")
    if title_ratio < TITLE_MIN or artist_ratio < ARTIST_MIN:
        return None
    if check_duration and delta > DURATION_MAX_DELTA:
        return None
    album_ratio = _ratio(album, record.album_name or "") if album else 0.0
    duration_ratio = max(0.0, 1 - delta / DURATION_MAX_DELTA)
    score = title_ratio * 0.5 + artist_ratio * 0.25 + album_ratio * 0.15 + duration_ratio * 0.10
    text, synced, instrumental = body
    return {
        "score": score,
        "text": text,
        "synced": synced,
        "instrumental": instrumental,
        "delta": delta,
        "plain": (record.plain_lyrics or "").strip(),
        "offset": 0.0,
        "align": None,
    }


def _call_lrclib(fn, **kwargs):
    for attempt in range(4):
        try:
            return fn(**kwargs)
        except NotFoundError:
            return None
        except RateLimitError:
            if attempt == 3:
                return None
            time.sleep(2 * (attempt + 1))
        except APIError:
            return None
    return None


def _better_match(scored, best):
    if scored is None:
        return best
    rank = (
        0 if scored["instrumental"] else 1,
        1 if scored["synced"] else 0,
        scored["score"],
        -scored["delta"],
    )
    if best is None:
        return scored
    best_rank = (
        0 if best["instrumental"] else 1,
        1 if best["synced"] else 0,
        best["score"],
        -best["delta"],
    )
    return scored if rank > best_rank else best


def _apply_onset(item, onset, length):
    if not item["synced"]:
        return
    decision = align_cues(cue_times(item["text"]), onset, length)
    item["align"] = decision["score"]
    if not decision["usable"]:
        if item["plain"]:
            item["text"] = item["plain"]
            item["synced"] = False
            item["align"] = None
        else:
            item["reject"] = True
        return
    if not decision["offset"]:
        return
    shifted = shift_synced(item["text"], decision["offset"], length)
    if not shifted:
        return
    item["text"] = shifted
    item["offset"] = decision["offset"]
    item["align"] = decision["score"]


def _pick_match(pool):
    usable = [item for item in pool if not item.get("reject")]
    if not usable:
        return None
    synced = [item for item in usable if item["synced"] and not item["instrumental"]]
    if synced and any(item["align"] is not None for item in synced):
        best_align = max(item["align"] or 0 for item in synced)
        close = [item for item in synced if (item["align"] or 0) >= best_align - 0.02]
        close.sort(key=lambda item: (abs(item["offset"]), -item["score"], item["delta"]))
        return close[0]
    best = None
    for item in usable:
        best = _better_match(item, best)
    return best


def find_lyrics(api, title, artist, album_artist, album, duration, length, onset=None):
    if not title or not artist or duration <= 0:
        return None

    artists = artist_queries(artist, album_artist)
    titles = title_queries(title)
    pool = []
    seen = set()

    def consider(record):
        if record is None or record.id in seen:
            return
        seen.add(record.id)
        scored = score_record(
            record, title, artist, album, duration, length,
            check_duration=onset is None,
        )
        if scored is None:
            return
        if onset is not None and abs(int(record.duration) - duration) > DURATION_MAX_DELTA and not scored["synced"]:
            return
        pool.append(scored)

    if album:
        for track_name in titles:
            for query_artist in artists:
                consider(_call_lrclib(
                    api.get_lyrics,
                    track_name=track_name,
                    artist_name=query_artist,
                    album_name=album,
                    duration=duration,
                ))

    for track_name in titles:
        for query_artist in artists:
            results = _call_lrclib(
                api.search_lyrics,
                track_name=track_name,
                artist_name=query_artist,
            )
            if not results:
                continue
            for record in results:
                consider(record)

    # Other edits (single, extended, remix) often use a longer title than the file.
    for query_artist in artists:
        results = _call_lrclib(api.search_lyrics, query=f"{title} {query_artist}")
        if not results:
            continue
        for record in results:
            consider(record)

    if onset is not None:
        for item in pool:
            _apply_onset(item, onset, length)
    return _pick_match(pool)


def metadata_moving(force_lyrics=False):
  ROOT_DIR = Path(r"D:\Lossless Music")
  BAD_CHARACTERS = "\\/:*?\"|<>"

  lrclib = LrcLibAPI(user_agent="local-musiclib/1.0.0 (https://github.com/local-musiclib)")
  saved_covers = set()

  # Collect all FLAC files (flat + already organized)
  flac_files = []
  for dirpath, _, filenames in os.walk(ROOT_DIR):
    for fname in filenames:
      if fname.lower().endswith(".flac"):
        flac_files.append(Path(dirpath) / fname)

  for item_path in flac_files:
    metadata = FLAC(item_path)

    file_name = sanitize_name(full_str=metadata["TITLE"][0] + ".flac", blacklist=BAD_CHARACTERS)
    track_number = metadata["TRACKNUMBER"][0].split("/")[0].zfill(3)

    album_name = metadata["ALBUM"][0].replace("/", "-").replace("\\", "-")
    album_name = sanitize_name(full_str=album_name, blacklist=BAD_CHARACTERS)

    track_folder = (track_number + " - " + file_name.split(".flac")[0]).replace("/", "-").replace("\\", "-")
    track_folder = sanitize_name(full_str=track_folder, blacklist=BAD_CHARACTERS)

    final_path = ROOT_DIR / album_name / track_folder / file_name

    # Move file if it's not already at the destination
    if item_path != final_path:
      if final_path.exists():
        print(f"SKIP (already exists): {final_path}")
        continue

      # Rename in place first if needed
      if item_path.parent == ROOT_DIR:
        renamed = ROOT_DIR / file_name
        if item_path != renamed:
          os.replace(src=item_path, dst=renamed)
        item_path = renamed

      os.makedirs(final_path.parent, exist_ok=True)
      os.replace(src=item_path, dst=final_path)

    dst_path = final_path

    # Save cover.jpg in album folder (once per album, extracted from embedded art)
    album_dir = ROOT_DIR / album_name
    if album_name not in saved_covers:
      saved_covers.add(album_name)
      cover_path = album_dir / "cover.jpg"
      if not cover_path.exists() and metadata.pictures:
        with open(cover_path, "wb") as f:
          f.write(metadata.pictures[0].data)
        print(f"Saved cover.jpg for {album_name}")

    # Skip tracks that already have lyrics unless this pass is forcing a refresh.
    lyrics_base = str(dst_path).rsplit(".", 1)[0]
    has_lyrics = os.path.exists(lyrics_base + ".lrc") or os.path.exists(lyrics_base + ".txt")
    if has_lyrics and not force_lyrics:
      continue

    try:
      title = _tag(metadata, "TITLE")
      artist = _tag(metadata, "ARTIST")
      album_artist = _tag(metadata, "ALBUMARTIST")
      album = _tag(metadata, "ALBUM")
      length = float(metadata.info.length or 0)
      duration = int(round(length))

      print(f"Searching lyrics for {artist} - {title}...")

      match = find_lyrics(lrclib, title, artist, album_artist, album, duration, length, onset_curve(dst_path))
      if match is None:
        print("  No confident match.")
        continue

      if match["instrumental"]:
        print(f"  Match (instrumental, duration delta {match['delta']}s).")
      elif match["synced"] and match["offset"]:
        print(f"  Match (synced, shifted {match['offset']:+.2f}s, duration delta {match['delta']}s).")
      else:
        kind = "synced" if match["synced"] else "plain"
        print(f"  Match ({kind}, duration delta {match['delta']}s).")

      is_synced = match["synced"]
      lyrics = match["text"]
      extension = ".lrc" if is_synced else ".txt"
      lyrics_path = Path(lyrics_base + extension)
      other_path = Path(lyrics_base + (".txt" if is_synced else ".lrc"))

      with open(lyrics_path, "w", encoding="utf-8") as f:
        f.write(lyrics)
      if other_path.exists():
        other_path.unlink()

      # Embed lyrics into the FLAC's LYRICS tag
      metadata = FLAC(dst_path)
      metadata["LYRICS"] = [lyrics]
      metadata.pop("UNSYNCEDLYRICS", None)
      metadata.save()
    except Exception as e:
      print(f"  Couldn't find lyrics: {e}")

  # Clean up empty folders
  remove_empty_folders(ROOT_DIR)

if __name__ == "__main__":
  metadata_moving()
