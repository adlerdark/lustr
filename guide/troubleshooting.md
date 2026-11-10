# Troubleshooting

## A video won't play, or stops

- Pick another quality in the player. **Direct** needs a browser that can play the file's format; any **transcoded** quality works with every file. See [Watching videos](player.md#quality-direct-or-transcoded).
- If the file itself is broken, mark it so it stops getting in the way: right-click → **⚠ Mark as corrupt**. See [Corrupt files](corrupt.md).
- If **all** videos fail, check that your video folders are still mounted on the server.

## Transcoding is slow or the CPU is busy

**Settings → Player** shows whether hardware encoding (VAAPI) was found. If it wasn't, lustr encodes on the CPU. To use an Intel or AMD GPU, pass `/dev/dri` to the container and add the host's render group; the [README](https://github.com/adlerdark/lustr#hardware-transcoding-intel--amd) shows how. Lowering **Highest transcode resolution** also helps.

## New files don't show up

lustr finds new files when it scans. Click **Rescan**, or turn on **Automatic library scans**. See [Scans & file changes](scans.md).

## A ⚠️ next to a library

Files were moved, copied or went missing. Click it to review them. See [Scans & file changes](scans.md#file-changes).

## No thumbnails, or no seek-bar previews

- Thumbnails: click 📷 in the top bar, or **Manage Library → Generate Missing Thumbs**.
- Seek-bar previews are made the first time a video is opened, or for a whole library under **Settings → Player → New player - seek-bar previews**.

## A search finds nothing

- Use quotes around values with spaces: `cast:"Jane Doe"`.
- A field value has to match the whole value. Use the full name, or a wildcard: `cast:jane*`.
- Check the filters that are still on: tags or sites in the sidebar, length, resolution, ★, ⏷ Filters and 👁 all narrow the results.
- See [Browsing & search](browsing.md#searching).

## Videos or libraries are missing

The 👁 display filter, or **Show libraries and videos** in Account & Security, may be hiding some library types or orientations. Videos marked corrupt are hidden too.

## Logged out unexpectedly

lustr logs out after a period of inactivity, when you close the browser, and after 30 days at most. See [Privacy & login](privacy.md).

## Forgot the password

On the server: `docker exec -it lustr python3 reset_password.py`.

## StashDB / ThePornDB doesn't work

Check that the API key is set (`STASHDB_API_KEY`, `TPDB_API_KEY`) and that the server can reach the internet. **Settings → External DB → Overview** shows which sources are working.

## Captions don't get made

If **Settings → Captions** says Whisper isn't installed, the image was built without it; rebuild it. Otherwise check the queue on that page for errors.

## Something else

The server log often says what went wrong: run `docker logs lustr` on the server.

If you think you found a bug, please open an issue on [GitHub](https://github.com/adlerdark/lustr/issues).
