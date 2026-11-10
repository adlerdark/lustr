# lustr

A self-hosted web app for organizing and streaming your personal adult video library.
Point it at your video folders, tag performers, sites and attributes, find anything with
filters and searches, and watch in the browser - on a desktop or a phone.

> **Status:** stable, in daily use and actively developed. Versions follow
> [semantic versioning](https://semver.org): 1.x upgrades keep your data, settings and setup
> (database changes apply by themselves); anything that would make you change your setup waits
> for 2.0. See [Releases](https://github.com/adlerdark/lustr/releases) for what's new in each
> version. An install has **one account** (the owner).

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
- No internet needed while watching: all front-end libraries ship with lustr (see
  [`static/vendor`](static/vendor/README.md)). Only getting the image, the update check and the
  optional external-database features go online.

## Install

### 1. Get the image

The image is built for you and published on GitHub's registry: `ghcr.io/adlerdark/lustr`.
The stack in step 3 already uses it, so there is nothing to do here, unless you'd rather pull it first:

```sh
docker pull ghcr.io/adlerdark/lustr:latest
```

`latest` is the newest version; `ghcr.io/adlerdark/lustr:1.0.0` (any version number) stays on that version.

<details><summary>Or build it yourself</summary>

```sh
git clone https://github.com/adlerdark/lustr.git
cd lustr
docker build -t lustr:latest .
```

Then use `image: lustr:latest` in the stack. (In Portainer you can also use **Images → Build a new image →
Upload** with a `.tar.gz` of the source.)
</details>

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
becomes the owner (password: at least 8 characters). Do this before you make the port
reachable by anyone else.

### 5. Add your videos

In lustr: **Manage Libraries → add a library → add folders**. You can pick folders under
`/media` (and `/media0`) - wherever you mounted your videos. lustr scans them right away;
length, codec and resolution fill in a moment later. Then make thumbnails with the 📷 button
(or Manage Library → Generate Missing Thumbs).

## User guide

Open **☰ → Help** at the top right of lustr (or press `?`) for the user guide: getting started,
browsing and search, the player, and more. The same pages are in [`guide/`](guide/getting-started.md)
if you'd rather read them here.

## Login and privacy

lustr always asks for the password, so nobody else in the house can open it by accident:

- **Logs out after inactivity**: 15 minutes by default (Settings → General). Playing a
  video pauses the countdown, unless you tick "Also while a video plays". When it logs
  out, the page reloads to the login screen, so nothing is left on screen.
- **Closing the browser logs out** too. Even with the timeout set to "Never", a login
  lasts at most 30 days.
- **Lock screen**: by default phones and media keys show only "lustr", not the video's
  title or picture (Settings → Player → Lock screen).
- After 5 wrong passwords from one device, that device has to wait 5 minutes.
- **Change the password** in Settings → General → Account. Other browsers that are
  logged in are logged out.
- **Forgot the password?** On the server, run:

  ```sh
  docker exec -it lustr python3 reset_password.py
  ```

  (`lustr` is the container's name.) It asks for a new password and logs every browser out.

If you reach lustr from outside your home, put it behind a reverse proxy with HTTPS
(lustr marks its login cookie as secure when the proxy sends `X-Forwarded-Proto: https`).

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
| `LUSTR_DATA_DIR` | `/app/data` | Where lustr keeps its data inside the container |
| `LUSTR_DRI_DEVICE` | `/dev/dri/renderD128` | GPU render device for VAAPI |
| `LIBVA_DRIVER_NAME` | auto | Force a VA driver (`iHD` for Intel, `radeonsi` for AMD) |
| `LUSTR_UPDATE_REPO` | `adlerdark/lustr` | GitHub repo the update check compares with (owner/name) |

## Your data and backups

The data folder holds:

- `library.db` - the database (libraries, metadata, collections, playlists, settings,
  login sessions)
- `users.json`, `watch_history.json`, `quicktags.json`, `metadata_schema.json`
- `cache/` - thumbnails and seek-bar previews (can be regenerated)
- `backups/` - automatic database copies (one per start, plus the ones made by
  **Settings → Tools → DB Maintenance**)

To back up, stop the container and copy the whole data folder. To move an install,
copy the folder to the new machine and mount it the same way (keep the same media paths
inside the container, e.g. `/media`, so your libraries still point at the right files).

Your video files are mounted **read-only**: lustr never changes or deletes them.

## Upgrading

When a newer version is out, an **Update available** notice appears at the bottom of lustr's sidebar
(the server checks GitHub about twice a day; turn it off in Settings → Maintenance).

- **Portainer**: open the stack → **Update the stack**, with **Re-pull image** switched on.
- **Compose**: `docker compose -f portainer-stack.yml pull && docker compose -f portainer-stack.yml up -d`

Your data folder is kept; database changes are applied automatically on start.
[Releases](https://github.com/adlerdark/lustr/releases) lists what changed in each version.

If you build the image yourself: `git pull`, `docker build -t lustr:latest .`, then recreate the
container (in Portainer, **Update the stack** with "Re-pull image" off). If your stack still says
`image: lustr:0.1.0` (from older instructions), change it to `image: ghcr.io/adlerdark/lustr:latest`,
or keep building with that tag.

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
