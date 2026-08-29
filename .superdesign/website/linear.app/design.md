---
version: "superdesign-alpha"
name: "Void Console"
description: "Near-black, product-screenshot-led dark system with a single indigo interactive accent, tight negative-tracked Inter display type, and glass utility controls floating over a near-void field."
colors:
  background: "#08090A"
  surface: "#0F1011"
  surface-alt: "#2C2D32"
  text-primary: "#F7F8F8"
  text-secondary: "#8A8F98"
  text-tertiary: "#62666D"
  border-subtle: "#23252A"
  border-hairline: "#FFFFFF"
  accent-interactive: "#5E6AD2"
  accent-signal-green: "#00FF05"
  accent-signal-orange: "#FF8849"
  accent-signal-cyan: "#74E3FF"
  accent-signal-yellow: "#F0BF00"
  accent-signal-pink: "#F79CE0"
typography:
  display-lg:
    fontFamily: "Inter Variable"
    fontSize: "64px"
    fontWeight: 510
    lineHeight: "1"
    letterSpacing: "-1.4px"
  headline-md:
    fontFamily: "Inter Variable"
    fontSize: "48px"
    fontWeight: 510
    lineHeight: "1"
    letterSpacing: "-1.1px"
  body-md:
    fontFamily: "Inter Variable"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: "1.6"
  label-md:
    fontFamily: "Inter Variable"
    fontSize: "16px"
    fontWeight: 590
    lineHeight: "1.5"
  body-default:
    fontFamily: "Inter Variable"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: "1.6"
  accent-mono:
    fontFamily: "Berkeley Mono"
    role: "inline code tokens, terminal/agent-log output, file paths"
spacing:
  base: "8px"
  micro: "4px"
  tight: "12px"
  gap: "22px"
  section-padding: "24px"
rounded:
  control-sm: "2px"
  control: "6px"
  card: "12px"
  input: "4px"
  pill: "9999px"
components:
  button-nav-cta:
    background: "#E5E5E6"
    text-color: "#08090A"
    radius: "9999px"
    height: "36px"
    hover-background: "#FFFFFF"
  button-primary-midpage:
    background: "#5E6AD2"
    text-color: "#F7F8F8"
    radius: "6px"
    height: "28px"
  button-glass-utility:
    background: "rgba(255, 255, 255, 0.05)"
    backdrop-filter: "blur(4px)"
    text-color: "#F7F8F8"
    radius: "9999px"
    height: "32px"
    padding: "0px 12px"
    shadow: "rgba(255, 255, 255, 0.03) 0px 0px 0px 1px inset, rgba(255, 255, 255, 0.04) 0px 1px 0px 0px inset, rgba(0, 0, 0, 0.6) 0px 0px 0px 1px, rgba(0, 0, 0, 0.1) 0px 4px 4px 0px"
    hover-background: "#191A1B"
  button-hero-primary:
    background: "#E5E5E6"
    text-color: "#08090A"
    radius: "9999px"
    height: "44px"
    padding: "0px 20px"
    border: "1px solid rgb(229, 229, 230)"
    shadow: "rgba(0, 0, 0, 0) 0px 8px 2px 0px, rgba(0, 0, 0, 0.01) 0px 5px 2px 0px, rgba(0, 0, 0, 0.04) 0px 3px 2px 0px, rgba(0, 0, 0, 0.07) 0px 1px 1px 0px, rgba(0, 0, 0, 0.08) 0px 0px 1px 0px"
    hover-background: "#FFFFFF"
    note: "observed exact — matches measured near-page-end button"
  button-secondary-glass:
    background: "rgba(255, 255, 255, 0.05)"
    backdrop-filter: "blur(4px)"
    text-color: "#F7F8F8"
    radius: "9999px"
    height: "44px"
    padding: "0px 20px"
    shadow: "rgba(255, 255, 255, 0.03) 0px 0px 0px 1px inset, rgba(255, 255, 255, 0.04) 0px 1px 0px 0px inset, rgba(0, 0, 0, 0.6) 0px 0px 0px 1px, rgba(0, 0, 0, 0.1) 0px 4px 4px 0px"
  card-feature-row:
    background: "transparent"
    radius: "6px"
    padding: "8px"
    shadow: "rgba(0, 0, 0, 0.4) 0px 2px 4px 0px"
  card-note-panel:
    background: "#0F1011"
    radius: "6px"
    padding: "12px 20px 16px 15px"
    shadow: "rgba(0, 0, 0, 0.2) 0px 0px 0px 1px"
  card-plain-content:
    background: "transparent"
    radius: "0px"
    padding: "0px"
---
# Void Console
Source: https://linear.app/

## Overview
This is dark-mode-default carried to its purest form: a near-black canvas (`#08090A`) that stays black for roughly nine-tenths of every screen, interrupted only by a live application chrome captured mid-workflow — issue trackers, PR diffs, chat panels, dot-plot charts — rendered as real UI screenshots rather than illustration. The aesthetic reads as Swiss-inflected product minimalism crossed with a developer-tool sensibility: tight negative letter-spacing on oversized Inter display type, a restrained one-hue interactive accent (`#5E6AD2` indigo), and glass-morphic floating controls (`blur(4px)`, `blur(20px)`) that imply a layered, elevated OS rather than a flat marketing page. Signal colors (green, orange, cyan, yellow, pink) exist but are locked inside the product screenshots as status dots and labels — never bleeding into page chrome. The system trusts negative space and real interface density to carry credibility, not gradients or illustration.

## Composition
The first screen is headline-over-void: a two-line display headline sits on bare black with nothing above it but a 73px transparent navbar, then a single muted subhead line and a small text-link utility to its right — no imagery yet. Below the fold, the hero's supporting proof arrives as a large embedded product screenshot (an issue-detail view with a floating agent chat panel overlapping its lower-right corner), it bleeds toward both edges suggesting a wider canvas cropped by the 1436px container. Scroll rhythm from there is a repeating unit: a numbered fragment label ("FIG"/section-index style caption), a two-column split (short display-weight title on the left, 2–3 lines of body copy plus a small numbered link on the right), then a full-width or near-full-width UI screenshot or diagram illustrating that claim — this pattern repeats at least four times (direction-setting, review, monitoring, and a fourth). A logo strip and one large two-tone statement paragraph (white lead-in, gray continuation) sit right after the hero, functioning as a credibility band before the feature rhythm begins. The page closes on a second bare-void headline moment mirroring the hero, then a dense multi-column footer. The deliberate choice: let real, dense application screenshots be the only imagery, rejecting decorative illustration or abstract gradient artwork as the primary visual — density and legibility of actual UI is the selling point, not atmosphere.

## Colors
Background `#08090A` covers roughly 73% of declared area and reads as pure black at a glance in the pixel field (~72% `#000000`, ~18% near-black `#181818`); this is the unambiguous page/surface-0. A slightly lifted panel tone `#0F1011` (~13% declared area) forms surface-1 for embedded UI screenshots and note cards. Text runs a three-step gray ramp: primary `#F7F8F8` (nearly white, for headlines and active copy), secondary `#8A8F98` (body/subhead), tertiary `#62666D` (deep-muted labels, timestamps). Borders are hairline: `#23252A` for structural dividers, `#FFFFFF` at near-zero opacity for glass edges. The single brand-interactive hue is `#5E6AD2` indigo, rationed to small mid-page controls only (a ~28px-tall button) — it never appears in the hero or footer. Signal hues `#00FF05`, `#FF8849`, `#74E3FF`, `#F0BF00`, `#F79CE0` are confined entirely inside product-screenshot chrome (status badges, chart dots, priority icons) and must never be promoted to page-level UI. Everything else — backgrounds, borders, most buttons — is deliberately left achromatic; color is a signal, not a decoration.

## Typography
Inter Variable carries the entire system at two structural weights: 510 for display/headline (64px and 48px, both at extremely tight tracking of ‑1.4px/‑1.1px and lineHeight 1) and 400/590 for reading and label text. Body copy sits at 15–16px with 1.6 line-height and secondary-gray coloring, giving long paragraphs a quiet, low-contrast feel against the near-white headlines. Labels (nav items, index numerals like small caption fragments) use 16px at weight 590, uppercase-adjacent in feel though not necessarily case-transformed. Berkeley Mono is the signature accent family — reserved strictly for inline code tokens, file-path strings, and terminal/agent-log text inside product screenshots, giving the developer-tool authenticity that pure Inter alone wouldn't supply.

## Layout
Content is capped at a 1436px max-width, centered. The repeating feature sections use a 2-column split (measured as a 2-column grid with rows at 100/100/18/81 — an asymmetric split where a short label/heading column sits beside a taller body+diagram column). A secondary pattern uses 2 columns at 16px gap with 3 items in a 100/47/47 arrangement — one full-width element over two half-width companions, likely a stat pairing beneath a heading. Card grids sampled mid-page are uniform single-column stacks disguised as rows: five stacked items at 100% width each (icon+body rows), then three at 100% each — vertical list rhythm, not multi-column bento, despite the marketing feel; treat these as **list layout**, not card grid, for rebuild fidelity. A first-screen icon triptych runs three equal 100%-width items in a row — a genuine 3-up uniform card grid. Spacing atoms are small and code-like: 4px, 8px, 12px, 22px, 24px — consistent with a product built by and for engineers, not a loose editorial rhythm. The footer is a five-column link grid (Product / Features / Company / Resources / Connect) against the same `#08090A` background, no card treatment, just plain text columns with a wordmark at the left.

## Components
- **Navbar**: fixed/sticky, 73px tall, transparent background with `backdrop-filter: blur(20px)`, 12 total items (logo + ~7 nav links + Log in + Sign up CTA + implicit search/edit icons). CTA is the pill button `#E5E5E6` fill, `#08090A` text, radius 9999px; a plain-text "Log in" sits to its left as the secondary action. Logo is a monochrome geometric mark paired with wordmark, left-aligned.
- **Hero primary button**: not present as a solid CTA in the hero itself — the hero instead carries only a small muted text-link utility ("New · Coding Sessions →") beside the subhead; no filled CTA sits directly under the headline on this page. Treat the two solid buttons that DO appear as prominent, near the page end, as the true primary/secondary pairing: `button-hero-primary` (`#E5E5E6` fill, `#08090A` text, 9999px radius, 44px height, layered soft shadow) and `button-secondary-glass` beside it (translucent `rgba(255,255,255,0.05)` + `blur(4px)`, 9999px radius, 44px height, inset+ambient shadow stack) — a filled/glass pair, not filled/outline.
- **Mid-page indigo button**: small utility control (28px tall, 6px radius, `#5E6AD2` fill) embedded within a product screenshot's chat/agent input — a compact, square-cornered send-style action, not a marketing CTA.
- **Glass floating panel (chat/agent overlay)**: appears layered atop the hero screenshot's lower-right corner; frosted `blur(4px)` translucent surface, pill-shaped internal send button, monospace log lines inside — this is the signature floating-glass motif repeated at multiple scroll depths (agent panel, mention dropdown, mini toolbar).
- **Feature content band** (×4 across the page, one per major claim): two-column layout — left a short 32–40px-scale heading over a small numbered index label, right 2–3 lines of secondary-gray body copy plus a small numbered anchor link; below, a full-bleed or near-full-bleed screenshot/diagram illustrates the claim. Internal media covers 60–80% of the band's vertical space.
- **Card-feature-row (list) family**: transparent fill, 6px radius, `rgba(0,0,0,0.4) 0px 2px 4px 0px` shadow, 8px padding; icon + body-text pairing stacked vertically at full width — used for compact feature/benefit lists inside a content band, not a card grid.
- **Note/annotation card** (e.g., a weekly-status panel): `#0F1011` surface, 6px radius, `rgba(0,0,0,0.2) 0px 0px 0px 1px` hairline shadow, asymmetric padding `12px 20px 16px 15px`; internally holds a heading, a small play/rate control cluster, and 2 status rows each with a colored status chip, an attribution line, and 1–2 bullet summaries.
- **Icon triptych** (first screen, 3-up uniform row): three equal-width, unpadded, radius-0 slots each carrying a single line-art isometric icon — decorative geometric figures, no text, functioning as a visual footnote beneath the credibility paragraph.
- **Logo strip**: single row of 8 monochrome wordmarks/glyphs, evenly spaced, white-on-black, no card container — a trust-signal band directly under the hero screenshot.
- **Footer**: `#08090A` background, hairline top divider, 43 links across 5 labeled columns, small wordmark at far left, legal links row beneath at reduced size/opacity.

## Graphics & Effects
The dominant "gradient" moments are narrow and functional, not atmospheric: a scrim `radial-gradient(52.53% 57.5% at 50% 100%, rgba(8,9,10,0) 0%, rgba(8,9,10,0.5) 100%)` sits under the hero product screenshot (covers ~7.5% of the page) to fade its base into the black page background — this is a media scrim, not a hero backdrop. A second gradient, `linear-gradient(rgb(8,9,10) 10%, rgb(208,214,224) 100%)`, also at ~7.5% coverage, transitions a dark-to-light-gray panel — likely the closing "Built for the future" band fading from black into a pale gray footer-adjacent zone, matching the visible gray gradient sweep above the final footer screenshot. Small radial highlights (`rgba(255,255,255,0.03–0.04)` circles fading to transparent, each ~0.8% of page area) sit inside glass buttons and cards as subtle top-light sheen, not visible page-wide. Shadows are used exclusively for elevation cueing at small scale: `rgba(0,0,0,0.4) 0px 2px 4px 0px` on list rows, `rgba(0,0,0,0.2) 0px 0px 12px 0px inset` for recessed panels. Backdrop blur appears at two strengths — `blur(20px)` for the sticky navbar's glass, `blur(4px)` for smaller floating controls and chat overlays. No visible grain or noise texture; the only "texture" is the literal density of screenshotted UI — code strings, dot-plot scatter charts, tiny status glyphs — which reads as a deliberate technical-authenticity texture in place of decorative pattern.

## Motion
Interactions are fast and precise, matching the developer-tool register: color transitions run at `0.1s ease` for near-instant text/icon state changes, while background fills ease over a slower `0.4s ease-out` for a deliberate, weighted feel on hover fills. Transform-driven entrances (scale/opacity/filter combined) use `0.16s` with `cubic-bezier(0.25, 0.46, 0.45, 0.94)` — a snappy ease-out with no overshoot, appropriate for toggling panels and glass overlays rather than bouncy marketing motion. A set of small keyframe animations (`grid-dot-*-upDown`) independently bob individual dots in a background dot-grid pattern up and down — a subtle ambient life effect confined to a decorative grid-of-dots layer, not applied to primary content.

## Guardrails
- Never let the hero carry a full-bleed saturated gradient — background stays black; gradients are narrow scrims under specific media elements only.
- Never promote signal colors (green/orange/cyan/yellow/pink) outside of embedded product-screenshot chrome into page-level buttons or headings.
- Never render the glass button/panel family as a flat solid — preserve the translucent `rgba(255,255,255,0.05)` fill plus `blur(4px)`/`blur(20px)` backdrop and its layered inset+ambient shadow stack.
- Never turn the stacked list-style "card" rows into a multi-column bento grid — they measure as full-width, single-column vertical stacks.
- Keep display type tightly tracked (‑1.1 to ‑1.4px) at weight 510 — looser tracking or heavier weight breaks the identity's restraint.
- Reserve indigo `#5E6AD2` for small, functional in-product controls only — it is not the marketing CTA color.