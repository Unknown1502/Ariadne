# Routes

**No router.** This is a single static route: `/` (or wherever the built `index.html` is
served — in production it's served by nginx from a Cloud Run service, proxying `/api/*` and
`/health` to a separate backend service; see `frontend/nginx.conf.template`).

| Path | Component | Notes |
|---|---|---|
| `/` (only route) | `src/App.tsx` | Entire app. Client-side "navigation" is just React state (`selectedId` picks which investigation to show; there is no URL change when switching investigations). |

`index.html`:
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Ariadne — AI Decision Under Investigation</title>
    <meta
      name="description"
      content="Ariadne tests whether an AI explanation deserves trust, under a declared intervention protocol. Synthetic laboratory; not a clinical system."
    />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
      rel="stylesheet"
    />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

Fonts already loaded: **Space Grotesk** (display, 500/600/700), **IBM Plex Sans** (body,
400/500/600), **IBM Plex Mono** (numbers/code, 400/500/600) — all via Google Fonts. No
favicon reference, no OG tags — worth adding as part of a "flawless" polish pass.
