# Editing metadata & bulk edit

Metadata is everything lustr knows about a video besides the file itself: its title, year, rating, cast, site, tags and the other fields. Searching, filtering and Home's suggestions all run on it.

## Edit one video

Right-click a video → **Edit Metadata**. You can also click **Edit** in the player's top bar, which pauses the video.

The window has three tabs:

- **General**: Title, Year, Rating, Movie, Orientation, Site, Cast, Male Cast and Tags.
- **Appearance**: Ethnicity, Nationality, Hair, Face, Body, Tits, Ass, Cock and Outfit.
- **Attributes**: Participants, Theme, Feature and Cumshot.

Fields that hold several values (cast, tags and the rest) take a **comma-separated list**, for example `Jane Doe, Ann Roe`. The buttons above a field are your [quick tags](quick-tags.md): click one to add it.

As you type, a list suggests the values that field already has in your library: values that start with what you typed first, then the rest, most-used first ("big t" finds `big.tits`). **↑** / **↓** pick one, **Enter** or **Tab** puts it in, and **Esc** closes the list. You can still type a new value. It works for each value in the list, not just the first.

**Save (Ctrl+Enter)** saves, and **Cancel (Esc)** closes without saving. Drag the bottom-right corner to make the window bigger; it remembers the size.

> Note: if you type a performer's name and several performers share it, lustr asks which one you mean.

### Male Cast

**Male Cast** is for the men in straight scenes. They stay in the video's cast and are searched like everyone else, but they're listed after the women and don't count when sorting by cast. The cast browser can show them separately.

### Movie

If the video is part of a full movie, type the movie's name in **Movie** (it suggests the ones you have), and the **Scene #** or **Part #**. Tick **Whole movie in this file** for a complete movie in one file. See [Movies](movies.md).

## Edit many videos at once

Select the videos (**Ctrl+click**, **Shift+click** or **Select All**), right-click one of them → **Edit Metadata**.

- The list fields show only the values **all** the selected videos share.
- A value you **add** is added to every selected video. A value you **remove** is removed from every one. Values only some of them have are left alone.
- Leave the Movie box empty to keep each video's own movie.

## Other right-click actions

| Action | What it does |
|---|---|
| **Merge Metadata** | Gives two or more selected videos the combined values of all of them. |
| **Clear Metadata** | Removes the title, year, rating and every list field from the selected videos. Length, codec and resolution stay. It can't be undone. |
| **Refresh Metadata** | Reads the length, codec, resolution and size from the files again. |
| **Apply Cast Attributes** | Adds each performer's primary (⭐) attributes, such as hair or ethnicity, to the videos they're in. Nothing is removed. See [Cast & performers](cast.md#attributes). |
| **Parse Filenames** | Fills in site, cast, year and title from the file names. See [Filename parser](filename-parser.md). |
| **Toggle Favorite** | Adds or removes the ★. |
| **File Info** | The file's path, size, codec, versions and play stats. |

## Browsing every value

🔍 **Metadata** in the top bar lists every value of every field with how many videos have it. **Grid View** and **Table View** change the layout. Click a value to see its videos. **🔬 Advanced Search** builds a search from several fields; see [Browsing & search](browsing.md#advanced-search).
