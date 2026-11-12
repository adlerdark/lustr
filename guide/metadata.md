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

New values are saved in one format: lowercase, with periods instead of spaces, so `Big Tits` is saved as `big.tits`. A value you already have keeps its spelling (typing `TEAMSKEETVIP` adds your `TeamSkeetVIP`). Cast, Male Cast, Title and Movie are saved as you type them. The same goes for album details and cast attributes.

**Save (Ctrl+Enter)** saves, and **Cancel (Esc)** closes without saving. Drag the bottom-right corner to make the window bigger; it remembers the size.

> Note: if you type a performer's name and several performers share it, lustr asks which one you mean.

### Tags

**Tags** is for labels about the video itself, rather than what happens in it (the other fields cover that: Theme, Feature, Outfit, Participants and so on). Use it for whatever you want to sort or filter your own collection by. Some ideas:

- Your own tiers, like `a.tier` and `s.tier`, so you can narrow your favorites down to your absolute favorites with one click in the sidebar.
- `full.movie` for a complete movie in one file (Find movies uses it too).
- The era or style: `vintage`, `semi.vintage`.
- The format or production: `vr`, `professional`, `homemade`.

Tags are listed in the sidebar's **Tags** tab like every other value, so a tag is always one click away as a filter.

### Rating

Ratings are 0.5 to 5 stars, in half stars. Click a star, or drag across them; drag to the far left, or click the rating a video already has, to remove it. With several videos selected, the stars set the rating of all of them, and dragging to the far left removes all their ratings. lustr asks as soon as you let go of the stars; **Cancel** leaves the rating alone, so each video keeps the rating it had when you save the rest. You can also rate the video you're watching in the player's top bar, next to the ♥ that makes it a favorite. Rated videos show their stars at the bottom left of their card, with the file type and length on the right; unrated ones show no stars. Filter by rating in the sidebar (see [Filtering](browsing.md#filtering)).

### Male Cast

**Male Cast** is for the men in straight scenes. They stay in the video's cast and are searched like everyone else, but they're listed after the women and don't count when sorting by cast. The cast browser can show them separately.

### Movie

If the video is part of a full movie, type the movie's name in **Movie** (it suggests the ones you have), and the **Scene #** or **Part #**. Tick **Whole movie in this file** for a complete movie in one file. See [Movies](movies.md).

## Edit many videos at once

Select the videos (**Ctrl+click**, **Shift+click** or **Select All**), right-click one of them → **Edit Metadata**.

- The list fields show only the values **all** the selected videos share.
- A value you **add** is added to every selected video. A value you **remove** is removed from every one. Values only some of them have are left alone.
- Leave the Movie box empty to keep each video's own movie.

### Clear a field on all of them

Type a single **`*`** in a field and save to empty that field on every selected video, whatever values each one had. It works for any field: the list fields (Tags, Cast, …) and the single ones (Title, Year, Movie). You're asked to confirm first.

In **Cast**, `*` removes everyone except the Male Cast; in **Male Cast**, only the Male Cast. Put `*` in both to empty the whole cast.

## Other right-click actions

| Action | What it does |
|---|---|
| **Merge Metadata** | Gives two or more selected videos the combined values of all of them. |
| **Clear Metadata** | Removes the title, year, rating and every list field from the selected videos. Length, codec and resolution stay. It can't be undone. |
| **Refresh Metadata** | Reads the length, codec, resolution and size from the files again. |
| **Apply Cast Attributes** | Adds each performer's primary (⭐) attributes, such as hair or ethnicity, to the videos they're in. Nothing is removed. See [Cast & performers](cast.md#attributes). |
| **Parse Filenames** | Fills in site, cast, year and title from the file names. See [Filename parser](filename-parser.md). |
| **Toggle Favorite** | Adds or removes the ♥. |
| **File Info** | The file's path, size, codec, versions and play stats. |

## Browsing every value

🔍 **Metadata** in the top bar lists every value of every field with how many videos have it. **Grid View** and **Table View** change the layout. Click a value to see its videos. **🔬 Advanced Search** builds a search from several fields; see [Browsing & search](browsing.md#advanced-search).
