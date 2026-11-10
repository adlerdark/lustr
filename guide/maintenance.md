# Maintenance & backups

**Settings → Maintenance** keeps the database tidy. It also runs by itself after every scan, so most of the time you don't need to visit it.

## Overview

The **Overview** shows the state of the database: how many videos and files, deleted files being kept, left-over data, the size of the backups, and when maintenance last ran.

- **🔍 Preview** shows what maintenance would do, without changing anything.
- **🧹 Run maintenance** does it. It backs up the database first.
- **Run + compact** also shrinks the database file. This takes about a minute.
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

## Import / Export

**Settings → Import / Export**:

- **📤 Export Metadata** saves the metadata of the library you're in, or of a search in All Videos, as **CSV** (opens in a spreadsheet) or **JSON**.
- **📥 Import Metadata** reads such a file back. Videos are matched by their file path.
