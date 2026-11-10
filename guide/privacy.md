# Privacy & login

lustr is built so that nobody else in the house opens your library by accident.

## One account

An install has one account, the owner. It's created on the first visit (**Create Account**). After that the login page only logs in.

## Logging out by itself

- **After inactivity**: 15 minutes by default. Change it in **Settings → Account & Security → Log out after inactivity**, or set it to never.
  - **Show countdown** shows the time left in the top bar.
  - Playing a video pauses the countdown, unless you tick **Also while a video plays**.
  - When the time runs out, the page reloads to the login screen, so nothing is left on screen.
- **Closing the browser** logs out too.
- Even with no timeout, a login lasts at most 30 days.

## The lock screen

Phones, media keys and the browser's media controls normally show what's playing. lustr shows only "lustr" there, never the video's title or picture. To show them anyway, tick **Settings → Player → Lock screen → Show the video's title and picture on the lock screen** (per browser).

## Passwords

- **Change it** in **Settings → Account & Security → Change password →**. Every other browser and TV that's logged in is logged out.
- **Forgot it?** On the server, run `docker exec -it lustr python3 reset_password.py`. It asks for a new password and logs everyone out.
- After 5 wrong passwords from one device, that device has to wait 5 minutes.

## What leaves your server

Nothing, unless you ask:

- Your video files are mounted read-only; lustr never changes or deletes them.
- Thumbnails, previews, captions and clips are all made on your server. Captions use Whisper locally.
- lustr only contacts StashDB or ThePornDB when you click something that searches them, and only if you've given it API keys.
- Everything the page needs (scripts, fonts) ships with lustr, so it works with no internet.

## Reaching lustr from outside

If you open lustr from outside your home, put it behind a reverse proxy with HTTPS. See the [README](https://github.com/adlerdark/lustr#login-and-privacy).
