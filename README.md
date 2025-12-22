# Discogs Style Enrichment Script

A tool that automatically tags your music files with style/genre information from Discogs. Perfect for DJs and producers who want better organization in their library.

## What This Does

1. **Scans your music folders** (including all subfolders)
2. **Parses filenames** to extract artist/album/title when ID3 tags are missing
3. **Searches Discogs** using the full filename and lets Discogs figure out what's what
4. **Writes tags from Discogs** including artist, title, album, style, and release ID
5. **Handles compilations** by matching track-level artist credits

**Supported formats:** MP3, AIFF

---

## Quick Start

```bash
# Test first (no changes made)
./run.sh "/path/to/your/music" --dry-run

# Run for real
./run.sh "/path/to/your/music"

# Limit to first N files (for testing)
./run.sh "/path/to/your/music" --dry-run --limit 15
```

**First time running?** The script will automatically create a virtual environment and install dependencies.

---

## Command Line Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without writing tags |
| `--limit N` or `-n N` | Only process first N files (useful for testing) |
| `--force` or `-f` | Re-process files that already have a STYLE tag |
| `--no-title-cleanup` | Skip the Bandcamp title cleanup step |
| `--verbose` or `-v` | Show detailed debug output |
| `--config FILE` | Use a YAML config file |
| `--token TOKEN` | Discogs personal access token |

---

## How It Works

### Filename Parsing

When ID3 tags are missing, the script parses the filename:

- `Artist - Title.aiff` → Artist + Title
- `Artist - Album - 01 Title.aiff` → Artist + Album + Title (track number stripped)

Track numbers (01-29) at the start of titles are automatically removed.

### Discogs Matching

The script searches Discogs using the **full filename** and scores results by checking if parts of the filename match:

- **Release artist matches** → +100 points
- **Track title matches** → +100 points
- **Track artist matches** (for compilations) → +100 points

A match needs at least **100 points** (one good match). When matched, the script trusts Discogs for the correct artist, title, and album names.

### Compilation Support

For compilation tracks like `Energy Rush - The Trip.aiff`:
- Searches Discogs for "Energy Rush - The Trip"
- Finds "The Trip - UK Business" where "Energy Rush" is a track
- Correctly tags: Artist = "The Trip", Title = "Energy Rush", Album = "UK Business"

---

## What Gets Written

| Tag | Source |
|-----|--------|
| `ARTIST` | From Discogs release artist |
| `TITLE` | From matched Discogs track |
| `ALBUM` | From Discogs release title |
| `STYLE` | Styles from Discogs (e.g., "Deep House; Techno") |
| `DISCOGS_RELEASE_ID` | Release ID for faster future lookups |

---

## Common Scenarios

### Test on a small batch first

```bash
./run.sh "/path/to/music" --dry-run --limit 20
```

### Re-process files with existing STYLE tags

```bash
./run.sh "/path/to/music" --force
```

### See detailed matching info

```bash
./run.sh "/path/to/music" --dry-run --verbose
```

### Process multiple folders

```bash
./run.sh "/path/to/folder1" "/path/to/folder2"
```

---

## Using Results in DJ Software

### Mp3tag

Add a STYLE column:
1. Right-click column headers → "Customize columns..."
2. Click "New" → Name: `Style`, Value: `%STYLE%`

### Rekordbox / Traktor

The STYLE tag is stored as a custom TXXX frame. You may need to copy it to GENRE:
1. In Mp3tag, select files
2. Actions → Format value → Field: `GENRE`, Format: `%STYLE%`

---

## Troubleshooting

### "Permission denied" when running ./run.sh

```bash
chmod +x run.sh
```

### "No Discogs results found"

- The track may not be on Discogs
- Try adding the Discogs release ID manually in Mp3tag (`DISCOGS_RELEASE_ID` tag)
- Run again with `--force`

### The script seems slow

Discogs limits requests to 60/minute. The script waits 1 second between requests. A large library will take time—run it overnight.

---

## Configuration File

Copy `config.example.yaml` to `config.yaml`:

```yaml
min_match_score: 100           # Minimum score to accept (default: 100)
title_cleanup_threshold: 0.90  # Similarity for Bandcamp cleanup
style_separator: "; "          # Separator between styles
request_delay: 1.0             # Seconds between API calls
```

---

## Files

| File | Description |
|------|-------------|
| `run.sh` | Launcher script (handles venv setup) |
| `discogs_enrich.py` | Main Python script |
| `config.example.yaml` | Example configuration |
| `CLAUDE.md` | Documentation for Claude Code |
| `.venv/` | Virtual environment (created automatically) |

---

## Credits

Built with:
- [python3-discogs-client](https://github.com/joalla/discogs_client) for Discogs API
- [mutagen](https://mutagen.readthedocs.io/) for ID3 tag reading/writing
