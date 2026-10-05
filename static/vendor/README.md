# Bundled front-end libraries

These files ship with lustr so the browser never has to load anything from the internet.

| File | Library | Version | Source | License |
|---|---|---|---|---|
| `tailwindcss-3.4.17.js` | Tailwind CSS (Play CDN build) | 3.4.17 | https://cdn.tailwindcss.com/3.4.17 | MIT |
| `alpinejs-3.13.3.min.js` | Alpine.js | 3.13.3 | https://cdn.jsdelivr.net/npm/alpinejs@3.13.3/dist/cdn.min.js | MIT |
| `video-8.24.1.min.js`, `video-js-8.24.1.min.css` | Video.js | 8.24.1 | https://cdnjs.cloudflare.com/ajax/libs/video.js/8.24.1/ | Apache-2.0 |
| `hls-1.7.3.min.js` | hls.js | 1.7.3 | https://cdn.jsdelivr.net/npm/hls.js@1.7.3/dist/hls.min.js | Apache-2.0 |
| `fonts/tangerine-700-latin.woff2` | Tangerine (bold, Latin) | Google Fonts v18 | https://fonts.google.com/specimen/Tangerine | SIL Open Font License 1.1 (`fonts/Tangerine-OFL.txt`) |

To update one, download the new version, change the file name and the reference in
`templates/index.html`, and update this table.
