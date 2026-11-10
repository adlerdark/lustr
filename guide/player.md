# Watching videos

**Double-click** a video to play it (on a phone, tap it). The player fills the screen; **Esc** or **✕** closes it.

## Picking up where you left off

lustr remembers how far you got in every video.

- Opened from **Continue Watching** on Home, a video starts where you stopped. A note says **Resumed at …**, with **Start over** if you'd rather begin again.
- Opened anywhere else, it starts from the beginning and offers **Resume** for ten seconds.

## Quality: Direct or transcoded

lustr can send a video two ways:

- **Direct**: the file as it is. Best quality, almost no work for the server, but the browser has to be able to play the file's format.
- **Transcoded** (Original, 1080p, 720p, 480p or 240p): the server converts it as it plays. Works for any file and any browser, and the lower qualities suit slow connections.

The label in the player's top bar shows which one is playing. Change it with **Quality** in the control bar (on a phone, the menu in the top bar). The quality you pick is remembered in that browser and used first next time. If a direct file won't play, lustr switches to a transcoded one by itself.

In **Settings → Player** you can choose:

- **Start with**: what plays first. The default plays direct when the browser can, and transcodes otherwise.
- **Highest transcode resolution**.
- **Use hardware transcoding**, if the server has an Intel or AMD GPU. That page shows whether one was found.

## Moving around

- Hover the seek bar to see a **preview** of that moment. Previews are made the first time you open a video, or for a whole library at once in **Settings → Player → New player - seek-bar previews**.
- **Strip** shows a row of frames under the video. Click one to jump there.
- Press **0**–**9** to jump to 0%–90% of the video.

### Keyboard shortcuts

| Key | Does |
|---|---|
| **Space** or **Enter** | Play / pause |
| **←** / **→** | Back / forward 10 seconds (**Shift**: 5 s, **Ctrl** or **Alt**: 1 minute) |
| **[** / **]** | Back / forward a tenth of the video |
| **0**–**9** | Jump to 0%–90% |
| **↑** / **↓** | Volume up / down |
| **M** | Mute |
| **F** | Full screen |
| **A** | Fit: switch between showing the whole picture and filling the screen |
| **L** | A-B loop: press once to set the start, again to set the end, a third time to stop |
| **T** | Make the current frame the video's thumbnail |
| **N** / **P** | Next / previous video in the playlist |
| **Esc** | Close the marker form, then the markers list, then the player |

### On a phone or tablet

- **Double-tap** the left or right side to go back or forward 10 seconds. Keep tapping to add 10 seconds each time.
- **Double-tap** the middle for full screen.
- A single tap shows or hides the controls.

## Markers and chapters

A **marker** is a named moment in a video, or a stretch between a start and an end.

- **+ Marker** in the top bar adds one at the current time. Give it a title or a tag, adjust **Start** (and **End** for a stretch) with **use current time**, then **Save**.
- **Markers (n)** lists them. Click one to jump there, or **Edit** it.
- If a video file has its own **chapters**, lustr turns them into markers the first time you play it (labelled *chapter*). To do this for every video at once, use **Settings → Player → Markers from chapters**.

## Multi-part videos

Some videos are split over several files (cd1, cd2…). Join them so they play as one:

1. Select the files (**Ctrl+click** each one).
2. Right-click → **Join as Parts (cd1, cd2…)**.
3. Check the order and the title, then confirm.

They become one video with one seek bar. The top bar shows which part is playing (**Part 1 / 2**). To undo it, right-click the video → **Unjoin Parts**.

## Playlists

When you play from a playlist, **◀ Prev** and **Next ▶** (or **P** / **N**) move through it. With **auto-next** ticked, the next video starts when one ends.

## Other buttons in the player

- **Edit**: the video's metadata, without leaving the player. The video pauses.
- **Thumbnail**: make the current frame the video's picture (same as **T**).
- **CC**: captions. If a video has none, the button offers to make them on the server.
- **✂ Clip**: save a short clip, starting with the next 10 seconds; you can adjust the start and end before saving. With an A-B loop set (**L**), it starts from the loop. Clips appear under **🎞 Clips** when they're ready; see [Clips](clips.md).
- **Classic player**: switch to lustr's original, simpler player. To use it every time, choose **Classic** under **Settings → Player → Which player**.

## Lock screen privacy

By default, phones, media keys and the browser's media controls show only "lustr", never the video's title or picture. To show them, tick **Settings → Player → Lock screen → Show the video's title and picture on the lock screen** (per browser).

## Play History

Every play is logged: when it was, how long you watched, and how far you got. Open it from **Settings → Play History**. It also keeps each video's play count and total time watched, which drive the **Most Played**, **Last Played** and **Time Watched** sorts. You can delete entries there, and their plays come off the totals.

## Open in VLC

Right-click a video → **Open in VLC** to play the file in VLC on your computer. This needs a one-time setup in **Settings → VLC**: tell lustr where your computer sees the video folders, and on Windows install the small link helper offered there.
