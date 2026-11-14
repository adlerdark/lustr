# StashDB & ThePornDB

**StashDB** and **ThePornDB** are online databases of scenes and performers. lustr can match your videos and performers against them and bring in titles, dates, sites, performers, tags, photos and attributes, so you don't have to type them.

> Note: nothing happens on its own. lustr only contacts these sites when you click something, and it never changes your metadata without showing you first, except for the "safe" values described under bulk matching.

## Setting up

Make a free account on the site(s), create an API key, and give it to lustr as an environment variable: `STASHDB_API_KEY`, and `TPDB_API_KEY` for ThePornDB. The [README](https://github.com/adlerdark/lustr#external-databases-optional) shows where. **Settings → External DB → Overview** shows which sources are working.

## Performers

On a performer's page, click **Ext DB**.

1. Pick **StashDB** or **ThePornDB**. lustr searches by their name and aliases; you can type another spelling, or paste a link or ID.
2. Click a result to see it. **Best match** marks the likeliest one. Double-click it, or select it and link it.
3. Once linked, the page shows their data from that site, with **View on site ↗**. **Suggest attributes** works out attributes from their data there (ethnicity, nationality, body, tits, ass); tick the ones you want. **Set Cast Photo** can take their photo.

**Fix match** links someone else instead, and **Clear link** removes the link.

For many performers at once, use **🌐 Bulk link…** in the cast browser. Sure matches are linked and the rest are kept for review. **✨ Bulk suggest…** then goes through attribute suggestions for many linked performers.

## Scenes

### One video

Right-click a video → **🌐 Find scene…**.

1. lustr first looks the file itself up on StashDB by its fingerprint (the same OSHash Stash uses, read from the start and end of the file). When StashDB users have that exact file, the scene shows as **same file (fingerprint)** and is almost certainly right. Otherwise it searches with what it knows: the title, performers, site and file name. You can also type a search, or paste a StashDB scene link.
2. Pick the right result (**Best match** marks the likeliest; hover a thumbnail to preview) and click **Link selected**.
3. **Suggest metadata** shows what the scene could add: title, date, site, performers, tags. Each is ticked or not according to your [tag mappings](tag-mappings.md). Change anything for this video only, add your own values under **Add your own**, then save.

**Fix match** and **Clear link** work as for performers.

### Many videos

Select videos and right-click → **🌐 Match scenes (n)…**, or click **🌐 Match scenes** in the top bar for the whole library.

Before it starts, it shows how many videos there are, how many are already linked, how many have a linked performer or site, and roughly how long it will take. You can skip videos that are already linked or were checked before. It runs in the background; the top bar shows its progress.

- **Strong matches are linked automatically**: a file whose fingerprint matches one StashDB scene, or a close match that several clues agree on (performers, studio, date, length, title). They get only safe values: the year and site (only when the video has none) and performers linked to your cast. Never tags, and nothing is removed. Auto-linked videos also take the scene's title.
- **Everything else goes to a review list.** Open it from the results, or with **🌐 Needs review** in the top bar's scene filter, and finish each one with Find scene.

## Sites

**Settings → External DB → Site Linking** links your sites to StashDB studios. Matching is better when lustr knows them.

- **Library default sites**: a site every video in a library is from.
- **Auto-link** (for the top 150, the top 300 or all of your sites not checked yet) links sites that match a studio exactly, or a network's parent studio. The rest are kept for review under **Needs a look (checked, not linked)**.
- You can give a site your own **filename abbreviations**, so the [filename parser](filename-parser.md) recognises them.

## Finding linked videos

- The top bar's 🌐 filter, or `scene:linked`, `scene:unlinked`, `scene:review` in the search box.
- **Settings → External DB → Linked videos** lists every linked video with its scene.

## Movies

ThePornDB also has full movies. See [Movies](movies.md#theporndb).
