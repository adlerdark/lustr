# Scans & file changes

lustr doesn't watch your folders all the time. A **scan** looks through a library's folders and compares them with what lustr knows: new files are added, and files that moved or disappeared are noted.

## Running a scan

- **Rescan** in the top bar scans the library you're in.
- **Manage Library** (the gear next to a library) → **Find new files** does the same. **Refresh Metadata** there re-reads every file's length, codec and resolution.
- Adding a folder to a library scans it right away.

### Automatic scans

**Settings → Libraries & Scans → Scans → Automatic library scans** runs scans on a schedule, for example every night at 4. Choose which libraries; with none ticked, all of them are scanned. Each scan also runs the database maintenance, like the Rescan button.

## File changes

When a scan finds files that moved, were copied or went missing, a **⚠️** with a number appears next to the library in the sidebar. Click it to review them.

There are three lists. Tick rows to act on several at once; with none ticked, the buttons above a list act on the whole list.

### Moved files

A file went missing, and a file with the same name and length turned up somewhere else, often in another library.

- **Merge**: it's the same video. The new file takes over the old one's metadata.
- **Separate**: they're different videos. Both are kept, and lustr won't pair them again.
- **Delete old**: throw away the old metadata.

### Duplicate files

Two files with the same name in different folders.

- **Keep both**: leave them as they are.
- **Delete first**: remove the first copy from lustr.

### Deleted files

Files that are gone, with nothing found to replace them. Their metadata is kept for a while (the page says how long), in case the file comes back or turns up somewhere else, and is then removed automatically.

- **Keep for now**: hide it until the next scan.
- **Delete now**: remove its metadata straight away.

> Note: "Delete" here only removes the entry and its metadata from lustr. lustr never deletes, moves or changes your video files. Before deleting or merging 10 or more entries, lustr backs up its database first.

## Videos in several files

- **Versions**: the same video in more than one file (say a 1080p and a 4K copy) can be one video with several versions. Right-click → **File Versions** shows them and which one plays.
- **Parts**: a video split into pieces (cd1, cd2) can be joined so it plays as one. See [Watching videos](player.md#multi-part-videos).
