# lustr

A self-hosted web app for organizing and streaming your personal adult video library.
Point it at your video folders, tag performers, sites and attributes, find anything with
filters and searches, and watch in the browser - on a desktop or a phone.

> **Status: 0.1.0, early release.** lustr works and is in daily use, but expect rough
> edges. For now an install has **one account** (the owner); multiple users with their
> own favourites and playlists are planned.

## Features

- **Libraries and groups** made from your folders; scans pick up new, moved and deleted files.
- **Rich metadata**: cast (with aliases), site, tags and attribute fields (hair, body,
  outfit, theme, …), quick-tag buttons, bulk editing, and a smart filename parser that
  learns from your corrections.
- **Search and filters**: field searches (`cast:jane*`, `site:(a,b)`, `path:2023`), tag and
  site filters, length filters, display filter by library type / orientation.
- **External databases**: match performers and scenes against StashDB and ThePornDB and
  pull in metadata (with your own tag mappings and pre-tick rules).
- **Home and Recommended**: Continue Watching, Recently Added, Recently Watched,
  "Because you watched …", Most Played, Favorites, Rediscover - per library too.
- **Collections** (hand-picked or saved filters that stay up to date) and **playlists**.
- **Player**: direct play or on-the-fly transcoding to 1080p / 720p / 480p / 240p, with
  hardware encoding (Intel / AMD VAAPI) or software; seek-bar previews, a scrubber strip,
  markers, keyboard shortcuts, playback speed, resume, double-tap seeking on phones.
  The original simpler player is still available.

## Requirements

- Docker (with Docker Compose, or Portainer), on a 64-bit **x86-64** machine.
- Optional: an **Intel or AMD GPU** for hardware transcoding (`/dev/dri`). Without one,
  transcoding runs on the CPU.
- The browser currently loads a few front-end libraries (Tailwind, Alpine.js, video.js,
  hls.js, a web font) from public CDNs, so the device you watch on needs internet access.

## Install

### 1. Build the image

```sh
git clone https://github.com/adlerdark/lustr.git
cd lustr
docker build -t lustr:0.1.0 .
```

(In Portainer you can also use **Images → Build a new image → Upload** with a `.tar.gz`
of the source.)

### 2. Make a data folder

Everything lustr keeps - the database, thumbnails, previews, accounts and settings -
lives in one folder on the host. Create it, e.g.:

```sh
mkdir -p /home/YOU/docker/lustr/data
```

### 3. Run it

Use [`deploy/portainer-stack.yml`](deploy/portainer-stack.yml) as a **Portainer stack**
(Stacks → Add stack → paste it) or with `docker compose -f deploy/portainer-stack.yml up -d`.
Edit the paths in it first:

```yaml
    volumes:
      - /home/YOU/docker/lustr/data:/app/data     # your data folder
      - /path/to/videos:/media:ro                 # your videos (read-only)
      # - /path/to/more/videos:/media0:ro         # optional second folder
```

Then open **http://&lt;your-server&gt;:8008**.

### 4. Create your account straight away

On a new install the first page is **Create Account**, and the first account created
becomes the owner. Do this before you make the port reachable by anyone else.

### 5. Add your videos

In lustr: **Manage Libraries → add a library → add folders**. You can pick folders under
`/media` (and `/media0`) - wherever you mounted your videos. lustr scans them right away;
length, codec and resolution fill in a moment later. Then make thumbnails with the 📷 button
(or Manage Library → Generate thumbnails).

## Hardware transcoding (Intel / AMD)

Uncomment the GPU lines in the stack and set your host's render group id:

```yaml
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - "109"          # getent group render | cut -d: -f3
```

**Settings → Player** shows whether hardware encoding (VAAPI) was found. If not, lustr
uses software encoding (libx264), which works everywhere but uses more CPU.

## External databases (optional)

To match scenes and performers against **StashDB** and **ThePornDB**, create API keys on
those sites and pass them as environment variables (in Portainer: "Environment variables"
under the stack editor; with compose: a `.env` file next to the stack file - see
[`deploy/.env.example`](deploy/.env.example)):

```
STASHDB_API_KEY=...
TPDB_API_KEY=...
```

## Settings by environment variable

| Variable | Default | What it does |
|---|---|---|
| `STASHDB_API_KEY` / `TPDB_API_KEY` | - | API keys for the external databases |
| `LUSTR_MEDIA_ROOTS` | `/media,/media0` | Folders inside the container that libraries can use |
| `LUSTR_STARTUP_BACKUPS` | `5` | Database copies kept in `data/backups` (one per start; `0` = off) |
| `LUSTR_SECRET_KEY` | generated | Key for login sessions; generated once into `data/.secret_key` if not set |
| `LUSTR_DATA_DIR` | `/app/data` | Where lustr keeps its data inside the container |
| `LUSTR_DRI_DEVICE` | `/dev/dri/renderD128` | GPU render device for VAAPI |
| `LIBVA_DRIVER_NAME` | auto | Force a VA driver (`iHD` for Intel, `radeonsi` for AMD) |

## Your data and backups

The data folder holds:

- `library.db` - the database (libraries, metadata, collections, playlists, settings)
- `users.json`, `watch_history.json`, `quicktags.json`, `metadata_schema.json`
- `cache/` - thumbnails and seek-bar previews (can be regenerated)
- `backups/` - automatic database copies (one per start, plus the ones made by
  **Settings → Tools → DB Maintenance**)

To back up, stop the container and copy the whole data folder. To move an install,
copy the folder to the new machine and mount it the same way (keep the same media paths
inside the container, e.g. `/media`, so your libraries still point at the right files).

Your video files are mounted **read-only**: lustr never changes or deletes them.

## Upgrading

```sh
cd lustr
git pull
docker build -t lustr:0.1.0 .      # use the new version number if it changed
```

Then recreate the container (in Portainer: the stack's **Update the stack** with
"Re-pull image" off, or **Recreate**). Your data folder is kept; database changes are
applied automatically on start.

## Notes

- **FFmpeg**: the image uses [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg)
  (GPL), downloaded while the image is built.
- **VLC helper**: Settings → VLC can download a small Windows script that registers a
  `vlc://` link handler (it writes to the registry), so "Open in VLC" works from the browser.
- lustr is meant for adults organizing their own, legally obtained videos.

## License

Copyright (C) 2026 Adler Dark

lustr is free software: you can redistribute it and/or modify it under the terms of the
**GNU Affero General Public License version 3** as published by the Free Software
Foundation. It is distributed WITHOUT ANY WARRANTY. If you run a modified version of lustr
for other people over a network, you must offer them its source code. See
[`LICENSE`](LICENSE) for the full text.
