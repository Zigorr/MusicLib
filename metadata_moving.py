from mutagen.flac import FLAC
from pathlib import Path
import syncedlyrics
import logging
import os

logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)

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


def metadata_moving():
  ROOT_DIR = Path(r"D:\Lossless Music")
  BAD_CHARACTERS = "\\/:*?\"|<>"

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

    # Fetch lyrics using syncedlyrics
    # Skip if a .lrc or .txt already exists for this track
    lyrics_base = str(dst_path).rsplit(".", 1)[0]
    if os.path.exists(lyrics_base + ".lrc") or os.path.exists(lyrics_base + ".txt"):
      continue

    try:
      title = sanitize_name(metadata["TITLE"][0], blacklist=BAD_CHARACTERS)
      artist = sanitize_name(metadata["ARTIST"][0], blacklist=BAD_CHARACTERS)

      print(f"Searching lyrics for {artist} - {title}...")

      lyrics = syncedlyrics.search(
        search_term=f"{artist} {title}",
        synced_only=False
      )

      if lyrics is None:
        print(f"  No lyrics found.")
        lyrics = "[Instrumental]"
        is_synced = False
      else:
        is_synced = lyrics.startswith("[") and not lyrics.startswith("[Instrumental]")
        print(f"  Found {'synced' if is_synced else 'plain'} lyrics.")

      extension = ".lrc" if is_synced else ".txt"
      lyrics_path = Path(lyrics_base + extension)

      with open(lyrics_path, "w", encoding="utf-8") as f:
        f.write(lyrics)

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
