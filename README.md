# MusicLib

A personal automation pipeline that takes newly downloaded music from a staging folder, enriches it with synced lyrics, and organizes it into a clean, structured music library — with zero manual tagging.

## Why I built this

As an audiophile, I care about having a fully organized, metadata-complete music library — every track properly tagged, every lyric synced, every file in the right place. Doing that by hand for a growing collection is tedious and error-prone, so I built a small pipeline to automate the entire process from "just downloaded" to "library-ready."

## What it does

MusicLib runs as a three-stage pipeline (`main.py` orchestrates all three):

1. **`directory_transfer.py`** — Moves newly downloaded audio files out of a staging/download folder and into the main music library structure.
2. **`metadata_enrichment.py`** — Fetches synced lyrics for each track using [`syncedlyrics`](https://pypi.org/project/syncedlyrics/) and prepares enriched metadata for embedding.
3. **`metadata_moving.py`** — Applies the enriched metadata (including synced lyrics) to each audio file and finalizes its placement in the library.

The result: drop new music into a staging folder, run the pipeline, and end up with a fully tagged, lyric-complete, well-organized library — no manual work required.

## Tech stack

- **Python**
- [**syncedlyrics**](https://pypi.org/project/syncedlyrics/) — for fetching time-synced lyrics

## Getting started

```bash
git clone https://github.com/Zigorr/MusicLib
cd MusicLib
pip install -r requirements.txt
python main.py
```

## Project structure

```
MusicLib/
├── main.py                  # Orchestrates the full pipeline
├── directory_transfer.py    # Moves files from staging folder to library
├── metadata_enrichment.py   # Fetches synced lyrics and prepares metadata
├── metadata_moving.py       # Applies metadata and finalizes file placement
└── requirements.txt
```

## Roadmap / possible extensions

- Pull additional metadata sources (album art, genre, release info) beyond lyrics
- Support duplicate detection before transfer
- Add a simple CLI or config file for customizing library folder structure
