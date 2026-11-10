# Captions

lustr can show captions (subtitles) in the player, in two ways:

1. **Subtitles that come with the file** are used as they are: a `.srt` or `.vtt` file next to the video (`name.srt`, `name.en.srt`), or a text subtitle track inside it.
2. Otherwise lustr can **make captions on the server** with Whisper, a speech-to-text model. Nothing leaves your server.

## Watching with captions

When a video has captions, the player's **CC** button turns them on and picks the language. If it has none, the CC button offers **CC: make captions**. The video goes to the front of the queue, and the CC menu appears once they're ready.

## How captions look

**Settings → Captions → How captions look** sets the look for everyone, in both players:

- **Size**, from 50% to 300%.
- **Text colour**, **Background** colour and **Background opacity** (0% is no box at all).
- **Edge**: **Outline**, **Drop shadow**, **Raised**, **Depressed**, or none. An edge keeps text readable without a background.
- **Font**: Sans-serif, Serif, Monospace or Casual.
- **Position**: at the **Bottom** or the **Top** of the picture (in the new player).

The preview shows the result as you change it, and a video that's open picks up the change straight away. **Reset** goes back to the defaults.

> Note: on an iPhone or iPad in full screen, the system's own caption settings can take over.

## Making captions for many videos

- Right-click videos → **Generate Captions**.
- **Manage Library → Generate Captions**: every video in the library that has none.
- With the **night window** (below).

Captions are made in the background at low priority, one video at a time. **Remove Captions** in the right-click menu deletes them.

## Settings

**Settings → Captions**:

- **Model**: bigger models are more accurate but slower. The default, **small**, is a good balance.
- **Language**: `auto` detects it per video; or give a code like `en`, `de` or `fr`.
- **CPU cores**: fewer cores make captions slower but leave more room for playback.
- **Use subtitles that come with the file**: on by default.
- **Make captions when I play a video that has none**.
- **Night window**: work through chosen libraries a little at a time during the hours you set.

The page also shows the queue, the video being worked on, and any that failed.

> Note: if the page says Whisper isn't installed, the image was built without it. Only subtitles that come with the files are found until you rebuild the image.

To find videos with or without captions, use the top bar's **⏷ Filters**, or search `captions:yes` / `captions:no`.
