# Theme

## Part 1 — Compact token summary

**No Tailwind. No CSS-in-JS.** Vanilla CSS with `:root` custom properties, one stylesheet
(`src/styles.css`, 816 lines). Single theme — no light mode, no `.dark` class, no
`prefers-color-scheme` handling anywhere.

### Colour — "chassis" (structural, currently dark/near-black)
| Token | Value | Use |
|---|---|---|
| `--ink` | `#0e1116` | page background |
| `--panel` | `#161a21` | card/section background |
| `--panel-raised` | `#1e242d` | hover / raised surface |
| `--graticule` | `#2a3039` | borders, dividers |
| `--graticule-soft` | `#21262e` | background grid lines (very faint) |

### Colour — type
| Token | Value | Use |
|---|---|---|
| `--text` | `#e6e9ee` | primary text |
| `--text-dim` | `#a8b2c1` | secondary text, numbers |
| `--muted` | `#8a94a3` | labels, captions |
| `--faint` | `#5c6675` | tertiary / de-emphasized |

### Colour — verdicts (**the only saturated colours in the whole system** — deliberate)
| Token | Value | Meaning |
|---|---|---|
| `--supported` | `#3fb6a8` (teal) | claim confirmed |
| `--contradicted` | `#e4572e` (orange-red) | claim refuted |
| `--inconclusive` | `#e0a82e` (amber) | insufficient evidence |
| `--supported-wash` | `rgba(63,182,168,0.12)` | tinted background for supported cards |
| `--contradicted-wash` | `rgba(228,87,46,0.12)` | tinted background for contradicted cards |
| `--inconclusive-wash` | `rgba(224,168,46,0.12)` | tinted background for inconclusive cards |
| `--thread` | `#3a4250` | the vertical timeline spine (neutral until a verdict colours it) |

### Typography
| Token | Stack | Role |
|---|---|---|
| `--font-display` | Space Grotesk → Segoe UI → system-ui | H1, verdict status, section titles, decision text |
| `--font-body` | IBM Plex Sans → Segoe UI → system-ui | paragraphs, captions |
| `--font-mono` | IBM Plex Mono → Cascadia Mono → Consolas | every number, hash, id, label, eyebrow — this is load-bearing: mono = "this is measured data," proportional = "this is prose" |

Type scale in use (no formal scale variable — ad hoc `font-size` per class):
- H1: `clamp(30px, 5.2vw, 52px)`, weight 700, letter-spacing `-0.025em`
- Verdict status: `clamp(28px, 4.2vw, 40px)`, weight 700
- Decision text: 26px, weight 600
- Debt total: 44px, weight 700
- Section title: 19px, weight 600
- Body: 15px base, line-height 1.55
- Labels/eyebrows: 10–11px, uppercase, letter-spacing 0.1–0.22em, mono

### Spacing / radius / misc
- `--radius: 3px` — nearly-square corners everywhere (deliberately instrument-like, not soft/app-like)
- `--spine: 22px` — left offset of the vertical "thread" timeline
- Card padding: 20px. Section top margin: 44px. Max content width: 1180px, centered.
- One `body::before` fixed full-bleed background grid (64px × 64px graticule lines, 0.35 opacity) — the only ambient decorative element on the page.
- Exactly one animation: `.spin` — a pulsing 8px dot used ONLY to indicate genuinely in-flight async work (never decorative). Respects `prefers-reduced-motion`.

## Part 2 — Raw source

### `frontend/vite.config.ts`
```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/health": { target: "http://localhost:8080", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
```

### `frontend/src/styles.css` — full file
See `frontend/src/styles.css` in the repo (816 lines) for the complete stylesheet — every
class referenced in `components.md`/`layouts.md` is defined there. Key structural note for a
redesign: **verdict colour propagates down the vertical thread** via
`.thread--{status} .stage--verdict ~ .stage::before` — i.e. every stage dot *after* the
verdict stage inherits the verdict's colour, which is the one piece of "state expressed as
structure" worth preserving in any redesign, however the visual language changes.

No `tailwind.config.*` exists (not a Tailwind project). No theme provider / context —
tokens are pure CSS custom properties read directly in `className`/inline `style`.
