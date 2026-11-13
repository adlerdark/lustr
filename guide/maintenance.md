# Maintenance & backups

**Settings → Maintenance** keeps the database tidy. It also runs by itself after every scan, so most of the time you don't need to visit it.

## Overview

The **Overview** shows the state of the database: how many videos and files, deleted files being kept, left-over data, the size of the backups, and when maintenance last ran.

- **🔍 Preview** shows what maintenance would do, without changing anything.
- **🧹 Run maintenance** does it. It backs up the database first.
- **Run + compact** also shrinks the database file. This takes about a minute.
- **Updates**: the version you're running and the newest one on GitHub. **Check GitHub for new versions** (on by default) and **Check now**. See [Updates](#updates) below.
- **Fix resolutions** re-reads every file so that vertical videos show their real resolution: 1080×1920 is 1080p, not 1920p. Resolutions you typed yourself are kept.

## The other pages

| Page | What it's for |
|---|---|
| **Deleted & moved files** | Files no longer found. They're kept for a number of days so that a moved file can be matched up and keep its metadata. Filter by library or reason; restore a file that exists again; open the moved files review. |
| **Performers with no videos** | Performers who appear in no video but still have a profile (photo, attributes, aliases, a StashDB link). Delete the ones you don't need. |
| **Same performer, different spelling** | Names that differ only in capitals, dots, spaces, apostrophes or accents. **Merge** a group into one name, or mark it **Not the same**. |
| **Gender check** | Performers whose gender or Male Cast mark doesn't fit the videos they're in. |
| **Tag cleanup** | Malformed values, nationality words stored as ethnicities, and sites spelled several ways. Everything is ticked; untick what you want to keep, then apply. |
| **Corrupt files** | Videos marked as corrupt. See [Corrupt files](corrupt.md). |
| **Backups** | See below. |

## Backups

lustr backs up its database:

- **Every time it starts.** It keeps the newest 5 (change this with `LUSTR_STARTUP_BACKUPS`).
- **Before big changes**: maintenance runs, bulk fixes, and deleting or merging 10 or more file changes. These safety backups are reused for 5 minutes, and the newest 5 are kept.
- **When you click Back up now** on the **Backups** page.

The **Backups** page lists them all with their date and size. **Keep the newest** *n* → **Delete the older** clears out old ones, or tick some and **Delete selected**.

The backups are in the `backups` folder inside lustr's data folder. To go back to one, stop lustr, copy it over `library.db`, and start lustr again.

> Note: a backup of the database holds all your metadata, but not thumbnails, previews or clips. To back up everything, stop lustr and copy the whole data folder. See the [README](https://github.com/adlerdark/lustr#your-data-and-backups).

## Updates

About twice a day the server asks GitHub for the list of lustr versions. When a newer one is out, an **Update available** notice appears at the bottom of the sidebar, under 🎞 Clips. Click it to open the new version on GitHub, then upgrade as the [README](https://github.com/adlerdark/lustr#upgrading) describes. Once you're running it, the notice goes away.

To check straight away, use **☰ → Check for Updates** in the top bar; the answer appears right in the menu. To turn the automatic check off, untick **Check GitHub for new versions** in **Settings → Maintenance → Overview**.

## Import and export

**Settings → Import / Export** lets you edit many videos' metadata in a spreadsheet (Excel, LibreOffice, Google Sheets), for example when you move over from a spreadsheet you kept before, or to fill in a column for a whole library at once.

1. Go to **Settings → Import / Export**. **Library** is **All Videos** at first; click it to pick a library or group instead.
2. Click **📤 Export CSV**. You get one row per video: its `path` and `filename`, then a column for each field.
3. Change the cells you want in the spreadsheet, and save it as **CSV**.
4. Click **📥 Import CSV…** and pick the file. Nothing is written yet: you see how many videos would change and what changes, for example `tags: +blonde, -brunette` or `title: Old → New`.
5. Click **Apply**. lustr backs the database up first, then writes only those changes.

**📄 Download template** gives you the same file with only the paths and filenames filled in, to start from empty columns.

### How the cells are read

- **A blank cell changes nothing.** You can delete every column you don't need; only `path` (or `filename`) has to stay.
- **List fields** (Tags, Cast, Hair, Site, …) hold several values separated by `|`: `blonde|curly`. For each list column, the import window lets you choose:
  - **add** (the default): the values are added; the video's other values stay.
  - **replace**: the cell becomes the whole list; values not in the cell are removed.
- **A lone `*`** clears the field.
- **title**, **year**, **rating** and **movie** (plus **movie_scene**, **movie_part**, **movie_whole**) replace what's there. **favorite** is `yes` or `no`.
- The technical columns (codec, length, resolution, …) are only there to help you sort; they're ignored on import, as are columns lustr doesn't know.
- Column names don't care about capitals or spaces: `Male Cast` is `male_cast`. Excel's semicolon-separated CSV works too.

Rows are matched to videos by `path`. If a path isn't found (the files moved, or you typed just a filename), a `filename` that only one video has is used instead; rows with a filename several videos share are skipped and listed.

### Full backup

**💾 Download full backup** (on the same page) downloads a zip with a fresh copy of the database (`library.db`: all your metadata, cast, movies, collections and settings) plus `quicktags.json` and `watch_history.json`. Your fields and their layout are in the database. Thumbnails, previews and clips aren't in it; lustr makes them again by itself. Logins (`users.json`) aren't in it either.

To restore it: stop lustr, unzip it into lustr's data folder (replacing `library.db` and the other files), and start lustr again. Each backup on the **Backups** page can also be downloaded on its own.
