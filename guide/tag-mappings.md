# Tag mappings

StashDB and ThePornDB have their own tags, and they don't use your names or your fields. A **tag mapping** says what one of their tags means in your library. For example, their "Big Tits" could become `big.tits` in your **Tits** field, and their "Outdoors" `outdoor` in **Tags**. Once a tag is mapped, every scene with that tag gets the same suggestion.

## How mappings are made

Mostly by using **Suggest metadata** (see [StashDB & ThePornDB](external-db.md#scenes)):

- A tag lustr can place by itself (it matches one of your values) is mapped **Auto**.
- **Unmapped tags** are listed underneath. Type your value(s) for one and it's saved as a mapping, or click **Never suggest this tag** to ignore it.
- When you apply a suggestion, its mapping becomes **Confirmed**.

## The Tag mappings page

**Settings → External DB → Tag mappings** lists them all.

- **External tag**: their tag. **Status**: **Auto**, **Confirmed** or **Ignored**.
- Your value(s) for it, and the field each goes in. A tag can stand for several values, comma-separated.
- **Videos**: how many of your videos have that value. **Uses**: how often you've applied it.
- **Pre-tick**: whether Suggest metadata ticks it for you. **Always**, **Never**, or only on some orientations. The default comes from **Settings → External DB → Overview**.
- **Per orientation**: a tag can mean something else on gay, bi, trans, lesbian or straight videos. Give it another value for those, or `-` for nothing.

Search with **Search tag or value…**. Add a mapping by hand with the **External tag** box at the top.

> Note: changing a mapping only affects suggestions from now on. It never changes videos you've already tagged.
