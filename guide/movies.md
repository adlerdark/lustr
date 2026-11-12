# Movies

Some videos are part of a full-length movie: a scene from it, a part of a DVD split over several files, or the whole movie in one file. lustr can keep track of which movie each video belongs to. The **Movies** tab then shows the movies themselves, with covers and details.

## Putting a video in a movie

In **Edit Metadata** (General tab):

- **Movie**: the movie's name. It suggests the ones you already have. Using another movie's name moves the video there.
- **Scene #**: which scene of the movie this is.
- **Part #**: which part, when the movie, or one long scene, is split over several files.
- **Whole movie in this file**: the file is the complete movie.

Right-click a video → **Add to movie...** to put it in a movie. Under the name box it suggests the movie its title names, the way **Find movies…** (on the Movies tab) reads titles: "Road Trip 3 Scene 2" suggests **Road Trip 3**, scene 2. Click the suggestion to use it.

With several videos selected, **Add to movie...** has two tabs:

- **One movie for all**: every selected video goes into the movie you type or pick.
- **By title**: the videos are grouped by the movie their titles name, each group with its scene numbers. Change a name if it's wrong, untick a group to leave it out, and click **Add**. A name that matches a movie you have adds to it; otherwise a new movie is made.

> Note: the same name is the same movie. "Jane's Diary 5" and "jane-s-diary-5" count as one. Two different movies that share a name can be told apart: right-click the videos of the second one → **Split into a new movie…**.

## The Movies tab

Each library has a **Movies** tab with the movies its videos belong to.

- **Search movies**, sort by **Title**, **Year**, **Most videos**, **Recently added** or **Link status**, and tick **Favorites first**.
- Filter by ThePornDB link: **Linked**, **Not linked**, **Needs review**, **Not found on ThePornDB**.
- Click a movie to open it. **‹ Movies** goes back.

## A movie's page

- **Front** and **Back** show the covers; **Change covers…** picks others. Right-click one of its videos → **Use as movie cover** to use that video's picture.
- **♥** marks a favorite.
- Its own details: **Name**, **Year**, **Studio**, **Director**, **Description**, and the **Cast** from ThePornDB. They belong to the movie and aren't copied onto its scenes. Edit them with **Edit**. A new name renames the movie for all its videos; another movie's name joins the two. The **Edition label** tells apart different movies with the same name, shown as "Name (label)".
- **Movie tags**: tags for the movie as a whole. The Movies tab's tag filter uses them, together with the tags of its scenes. **Tags → scenes…** copies some or all of them onto the movie's videos.
- Its videos. Right-click → **Remove from** *(the movie)* takes one out.

## ThePornDB

With a ThePornDB API key (see the [README](https://github.com/adlerdark/lustr#external-databases-optional)), lustr can fill in a movie's covers, details, cast and tags:

- **Link to ThePornDB…** looks up every unlinked movie in the library. Sure matches are linked; the rest go to **Needs review** for you to check.
- **Find movies…** looks through your video titles, file names and scene links for movies you haven't set up yet, and lets you review them before anything is changed.
- On a linked movie, **open on ThePornDB ↗** opens its page there.

When you add tags from ThePornDB, each of its tags can be mapped once to one of your fields and values. The mapping is then used for movies and scenes alike.
