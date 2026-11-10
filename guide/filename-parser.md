# Filename parser

Many video files already say what they are: `examplesite.24.03.15.jane.doe.and.ann.roe.beach.day.mp4`. The filename parser reads those names and proposes the site, performers, year and title, so you don't have to type them.

## Open it

- For some videos: select them, right-click → **Parse Filenames**.
- For a whole library or group: the library's gear icon (**Manage Library**) or the group's gear → **Parse Filenames**.

Under **Selected files** / **Library or group** you can switch what it works on.

## Smart mode

**Smart** works out the names on its own. It uses the performers and sites you already have, your libraries' default sites, and studios from your cached StashDB scenes. It only proposes what a video **doesn't have yet**: a site, performers, a year, and a title while the title is still the file name. Nothing is replaced.

## Pattern mode

When your files all follow one layout, **Pattern** reads them exactly. Type a pattern, or click the pieces below the box to add them at the cursor:

| Piece | Reads |
|---|---|
| `%S` | Site |
| `%C` | Cast: one or more performers |
| `%MC` | Male Cast: one or more performers |
| `%F`, `%M`, `%L` | A first, middle and last name |
| `%T` | Title: everything up to the next part of the pattern |
| `%Y` / `%YY` | Year, 4 or 2 digits |
| `%MM`, `%DD` | Month, day |
| `%R` | Resolution, e.g. `1080p` |
| `*` | Anything; it's skipped |

Some examples:

- `%S.%YY.%MM.%DD.%C.%T`: `examplesite.24.03.15.jane.doe.beach.day`
- `%C - %T`: `Jane Doe - Beach Day`
- `* %C and %MC - %T`: anything, then the women, "and", the men, a dash and the title.

Names in a cast list are split on commas, and on "and", "/" or "&" with spaces around them.

The options under the pattern tidy the result: **No Underscores**, **No Dashes**, **No Dots**, **Title Case**, and **Put the folder name at the start of the title** (handy for series: a file "Episode 4" in the folder "Season 2" gets the title "Season 2 Episode 4"). With selected files, the preview shows what the first one becomes.

## Review and apply

Click **Parse the selected files** (or **Parse** and the library name) to see the proposals, a row per video:

- **Untick** anything you don't want. **Tick all** picks every proposal of one kind.
- If the parser got the kind wrong, for example a site read as a performer, **change its field**. Click a value to correct its spelling.
- **new** marks a performer or site you don't have yet. **which one?** appears when several performers share a name; pick the right one.
- **Apply ticked** saves them.

The parser **remembers your corrections**, so the next run gets them right. **Remembered corrections** lists them; **×** forgets one.
