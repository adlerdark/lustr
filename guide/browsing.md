# Browsing & search

## Home

**Home**, at the top of the sidebar, shows rows of suggestions:

- **Continue Watching**: videos you stopped part-way through.
- **Recently Added**, **Recently Watched**, **Most Played**, **Favorites**.
- **Because you watched…**: videos you haven't played that share cast, site or tags with something you played lately.
- **Rediscover**: a random handful of videos you have never played.
- **Collections on Home**: collections you chose to show here.

Click **⚙ Customise** to choose which rows show and in what order. **See all ›** on a row opens its full list.

Home has the same tabs as a library. **Recommended** is Home itself; **Library**, **Collections**, **Movies** and **Images** open those tabs of **All Videos**, so every collection, movie and album is one click away.

## Libraries, groups and All Videos

Click a library in the sidebar to see its videos, or a group to see all its libraries together. **All Videos** shows everything. **Total** and **Filtered** under the list tell you how many videos the library has and how many your filters leave.

## Library tabs

A library (or group, or All Videos) has tabs above its videos:

- **Recommended**: the same kind of rows as Home, for this library only.
- **Library**: the videos themselves. This is where the filters, search and views below apply.
- **Collections**: see [Collections & playlists](collections.md).
- **Movies**: see [Movies](movies.md).
- **Images**: the pictures in the library's folders. See [Images](images.md).

## Views

The view buttons in the top bar switch between:

- **Thumbnails**: a grid of pictures.
- **List**: a table. Right-click the column headers to choose the columns; drag their edges to resize them.
- **Folders**: the library's folders as they are on disk. Click a folder to go in; the path bar above the videos takes you back up.

A library can open with its own view, sort and tab. Set this in **Settings → Libraries & Scans → Libraries**, or for every library in **Settings → Browsing**.

## Sorting

Pick a field in the **Sort** menu; the arrow next to it switches between ascending and descending. Besides the metadata fields there are **Recently Added**, **Most Played**, **Last Played**, **Time Watched** and **Random**.

## Filtering

All filters add up: a video has to pass every one of them to show.

| Filter | Where | What it does |
|---|---|---|
| Tags | sidebar, **Tags** tab | Click: only videos with the tag. Click again: hide them. Third click: clear. Right-click: hide. |
| Sites | sidebar, **Sites** tab | The same as tags, for sites. |
| Length | sidebar | Buttons from **<2 min** to **40+ min**. **Settings → Browsing** can switch them to sliders. |
| Resolution | sidebar | **4K**, **HD**, **SD High**, **SD Low**: the lowest quality to show. |
| Rating | sidebar, under Resolution | Click or drag the stars to the lowest rating to show: 4 stars shows 4 and up. **✕** (or dragging to the far left) shows every video. |
| ♥ | top bar | Favorites only. |
| ⏷ **Filters** | top bar | With or without captions, chapters in the file, or markers. |
| 🌐 | top bar | Videos linked to a StashDB / ThePornDB scene, not linked, or needing review. |
| 👁 | top bar | Which library types and orientations are shown, just for you. |

The **×** next to the Tags / Sites tabs clears both. The filter box above each list narrows it; press **Enter** to include the first match.

## Searching

Type in the search box at the top and press **Enter**. Plain words search titles, filenames, folder paths, cast and tags. **×** in the box clears the search.

### Search by field

Put the field name, a colon and the value, with no spaces around the colon:

| You type | You get |
|---|---|
| `cast:"Jane Doe"` | Videos with that performer. Use quotes when the value has a space. |
| `site:examplesite` | Videos from that site. |
| `tags:outdoor` | Videos with that tag. |
| `hair:blonde`, `outfit:lingerie`, … | Any metadata field: `ethnicity`, `nationality`, `hair`, `body`, `tits`, `ass`, `face`, `cock`, `outfit`, `theme`, `feature`, `cumshot`, `participants`, `orientation`. |
| `tags:(outdoor,pool)` | Either value: a list in brackets, separated by commas. |
| `path:2023` | Files whose path contains `2023`. |
| `library:Archive` | Only that library (or group). Combine with other terms. |

Values are not case-sensitive. A field value has to match the whole value, so either type the full name or use a wildcard.

Fields you added or renamed in **Settings → Metadata → Fields & editor layout** work the same way: type the field's name with `_` for spaces (`studio_code:abc`, `hair_colour:red`), or its original key, which never changes (`hair:red` still works after renaming Hair). A hidden field finds nothing.

### Wildcards

`*` stands for any characters (`%` works too):

| You type | You get |
|---|---|
| `cast:jane*` | Performers whose name starts with "jane". |
| `site:*tube` | Sites ending in "tube". |
| `tags:*door*` | Tags with "door" anywhere in them (indoor, outdoor…). |
| `tags:(out*,pool)` | Wildcards work inside lists too. |
| `path:clips/*2023` | Paths with "clips/" followed somewhere later by "2023". |

### Combining terms (AND, OR, NOT and brackets)

You can combine any terms (field terms like `tags:outdoor`, plain words, `"a phrase"`) with **AND**, **OR**, **NOT** and round brackets, as deep as you like. The words can be in capitals or not.

| Write | Means |
|---|---|
| `tags:outdoor beach` | Both. Terms next to each other must **all** match; `AND` between them says the same. |
| `tags:outdoor AND beach` | The same. |
| `hair:red OR tags:redhead` | Either one (or both). |
| `NOT tags:vr` | Every video **without** that tag. |
| `NOT(tags:vr)`, `-tags:vr` | The same: `NOT` with or without brackets, or a minus sign stuck to the front of a term. |
| `(hair:red OR hair:blonde) NOT tags:vr` | Brackets group terms: red or blonde hair, and not VR. |

**Which goes first.** `NOT` applies only to the term (or bracket) right after it. `AND` comes before `OR`, so `a OR b c` means `a OR (b AND c)`. When in doubt, add brackets: `(a OR b) c`.

Some longer examples:

- `(tags:s.tier OR tags:a.tier) AND (cast:"Jane Doe") AND (NOT(tags:vr))`: your top tiers, with Jane Doe, but not VR.
- `(tags:s.tier OR tags:a.tier) AND (cast:"Jane Doe" AND NOT(tags:vr))`: the same, grouped another way.
- `site:examplesite (outdoor OR pool) -(hair:red OR tags:vintage)`: one site, outdoors or by a pool, leaving out red hair and anything vintage.
- `tags:full.movie NOT (tags:vintage OR tags:semi.vintage)`: full movies that aren't vintage.

Good to know:

- The same field twice means both: `tags:a tags:b` finds videos with both tags. For **either**, use `OR` or a list: `tags:(a,b)`.
- A plain word in a boolean search looks in the title, the file path (name and folders) and every field's values, including cast and tags.
- `library:`, `res:`, `stars:`, `movie:` and the other [special keywords](#special-keywords) work inside brackets too, for example `NOT library:Archive` or `(res:4k OR stars:5)`.
- A hyphen inside a word, like `semi-vintage`, is just part of the word; only a `-` at the start of a term means NOT.

### Special keywords

| You type | You get |
|---|---|
| `res:4k`, `res:hd`, `res:sdhigh`, `res:sdlow` | That quality or better (like the Resolution filter). |
| `stars:4`, `stars:3.5` | Rated that many stars or more (like the Rating filter). |
| `added:2024-03` | Added to lustr in March 2024. Also `added:2024` or `added:2024-03-15`. A date alone, like `2024-03`, works too. |
| `modified:2024` | Files last changed in 2024. |
| `captions:yes` / `captions:no` | With or without captions. |
| `chapters:yes` / `markers:yes` | With chapters in the file / with markers. `no` for without. |
| `scene:linked`, `scene:unlinked`, `scene:review` | By StashDB / ThePornDB scene link. |
| `movie:yes`, `movie:no`, `movie:"Title"` | Videos that are part of a movie, that aren't, or of one movie. |

### Advanced Search

For bigger searches, open 🔍 **Metadata** → **🔬 Advanced Search**. Type search terms, search titles only, and click values from any field to add them. **AND** / **OR** sets whether a video needs all the values you picked or any of them. **Search in** sets where it looks: **All Videos** (the default), or **Select libraries…**, which opens your libraries and groups in the same order as the sidebar (click ▾ to fold a group) to tick the ones to search. With one ticked, the search opens in that library or group; with several, in **All Videos**, limited to them (`library:(A,B)` in the search box). **🔍✏️ Edit Search** in the top bar brings the form back.

**★ Save as smart collection…** at the bottom runs the search and saves it as a [smart collection](collections.md), so you can come back to it any time.

## Selecting videos

**Click** selects one video, **Ctrl+click** adds or removes one, and **Shift+click** selects everything between two videos. **Select All** selects the page, then offers to select every video the filters show. Right-click a selected video to act on all of them at once: edit their metadata, add them to a playlist, and so on.

## Paging

By default more videos load as you scroll. **Settings → Browsing** can switch to a **Load More** button, page numbers, or loading everything at once.
