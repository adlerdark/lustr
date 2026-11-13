# Settings reference

Open Settings with ⚙ at the top right. Pages that a guide page explains have a **? Help** link next to their title.

> Note: some settings are kept **in this browser** (the page says so), so another device can be set up differently. The rest are saved on the server and apply everywhere.

## Personal

**Account & Security**: your password, logging out after inactivity, linking a TV, and **Show libraries and videos**, which hides library types and orientations just for you. See [Privacy & login](privacy.md) and [Link a TV](tv.md) (for the [lustr-tv](https://github.com/adlerdark/lustr-tv) app).

**Appearance**: which **Header buttons** show in the top bar, the **Settings layout** (a full page or a popup window), and the **Theme**:

- **Light themes**: Light, Paper (warm, off-white) and Mist (cool grey-blue). The players, the image viewer and the badges on pictures stay dark in them.
- **Dark themes**: Dark (Default), Dusk (a softer, mid-tone dark), Dark Ember, Midnight Forest, Volcanic, Arctic, Noir, Sakura, Synthwave, Deep Sea and Aurora.
- **Upload Custom Theme**: your own `.css` file. A theme is just one block of colour variables. Download `default.css` or `light.css` from that section, change the colours, and upload it; the comment at the top of the file explains each one.

The theme is saved in this browser.

**Browsing**: see [Browsing & search](browsing.md). It has four parts:

- **General**: **Show libraries and videos**, **Same-name performers: show their number** (on cards, rows and in the player), and **Sidebar: Hide Unavailable Tags / Sites** instead of greying them out.
- **Library tab**: **Videos per page** and how pages work; **When a library opens** (its starting sort, view and tab; each library can override this); **Configure Columns** for the list view; the length filter's **Slider Mode** and **Default**.
- **Movies tab**: **Movies per page** and how pages work.
- **Images tab**: **Albums per page** and **Photos per page**, how pages work, and **Folders to skip**.

Each tab has the same four ways to page: **Pages** (Previous / Next, only the current page shows), a **Load More button**, **Infinite scroll**, or **Show everything**. An open collection follows the Library tab's choice.

## Library

**Libraries & Scans**: see [Getting started](getting-started.md) and [Scans & file changes](scans.md).

- **Libraries**: create, rename, delete and order libraries, add folders, and set each one's default sort, view and tab.
- **Groups**: groups of libraries in the sidebar.
- **Library settings**: **Hide 'All Videos' from Sidebar**, and the **Orientation types** of groups and libraries, which decide what **Show libraries and videos** hides.
- **Scans**: **Automatic library scans**, **Library and group types**, and **Apply performers' starred attributes when they're added to a video**.

**Thumbnails**: **Preview Frames** and **WebP Quality** for thumbnails.

**Metadata**: **Fields & editor layout** (below) and **Edit Quick Tags** (see [Quick tags](quick-tags.md)).

### Fields & editor layout

**Settings → Metadata → Fields & editor layout** lets you shape the fields to your library instead of the ones lustr comes with. Everything follows what you set: Edit Metadata, album details, search and Advanced Search, the sidebar, list-view columns and sorting, import / export, quick tags, performer attributes and tag mappings.

- **Arrange**: each tab is a column. Drag a field to reorder it or move it to another tab. **+ New tab** adds a tab; ✎ renames one, ‹ › move it, ✕ removes an empty one. General always comes first, and its 🔒 fields (Orientation, Site, Cast, Male Cast, Tags) always stay on it. Title, Year, Rating, Resolution and Movie are at the top of General and aren't in the list.
- **Rename**: click a field and change its **Name**. Only the name you see changes, never the values. A field also keeps its original key, so a search like `hair:red` keeps working after you rename Hair.
- **Single-word values**: on, new values are saved lowercase with periods (`Big Tits` → `big.tits`) and the field is one of the sidebar's filters. Off, spaces are allowed (good for free text, like notes) and the field isn't a sidebar filter. Turning it on doesn't change the values already there; lustr tells you how many don't match.
- **Performer attribute**: offered in a performer's attributes, so their primary values can be added to their videos (see [Cast & performers](cast.md)).
- **New field**: give it a name, pick its tab and whether it takes single words. It's a list field like Tags: several values per video.
- **Hide field**: the field disappears from everywhere above, but **its values are kept**. It's listed under **Hidden fields**; **Restore** brings it back with all its values. Searching a hidden field finds nothing.
- **Revert to default**: tabs, order, names and options go back to how lustr comes, and hidden built-in fields come back. Fields you added are hidden, not deleted.
- **Advanced → Delete…** (fields you added only): erases the field's values from every video and album, its performer attributes, quick tags and tag mappings. lustr shows how much would go, makes a backup of the database first, and asks you to type the field's name. lustr's own fields can only be hidden.

Your fields and their layout are stored in the database, so restoring a backup brings back the fields that go with it.

**Play History**: the log of everything played, and the **Minimum play length** that counts as a play. See [Watching videos](player.md#play-history).

## Playback

**Player**: which player (**New** or **Classic**), the lock screen, transcoding and hardware encoding, the starting quality, seek-bar previews, and markers from chapters. See [Watching videos](player.md).

**Captions**: how captions are made and **How captions look** (size, colours, edge, font, position). See [Captions](captions.md).

**VLC**: for **Open in VLC**. Each row maps a **Server path** to the **Local path on this computer**, for example `/media/videos` to `Z:\Videos`. On Windows, download the link helper and run it as Administrator once.

## Data

**External DB**: StashDB and ThePornDB. **Overview**, **Tag mappings**, **Site Linking** and **Linked videos**. See [StashDB & ThePornDB](external-db.md) and [Tag mappings](tag-mappings.md).

**Maintenance** (including **Updates**, the check for new versions) and **Import / Export**: see [Maintenance & backups](maintenance.md).
