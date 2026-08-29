# Ariadne Console — Design System v3 ("Instrument", Linear-DNA)

## Why v3 exists

v1 (light editorial) and v2 (light-with-dark-rail "Control Room") were both rejected as
still reading like a template. Asked directly for a real reference, the user pointed at
Linear — extracted from the live site (`.superdesign/website/linear.app/design.md`). Linear's
actual site is dark. The user's original objection was never darkness itself; it was the
**generic AI-wrapper version of dark** — black background + purple/blue glow gradient +
glassmorphism, the look every AI hackathon project already has. Linear proves those are not
the same thing: it is near-black, restrained, and reads as premium engineering software
precisely because of what it *refuses* to do — decorative gradients, multi-hue glow, loose
type. v3 adopts that restraint, not Linear's literal palette.

## Direction

**An instrument panel, not a marketing page.** Near-void background. Exactly ONE interactive
accent color, used sparingly, never as decoration. Real data — the verdict, the live counters,
the evidence chart — is the only "visual interest" on the page; there is no illustration, no
gradient artwork, no glow to manufacture energy that the content should be providing itself.
Confidence comes from typographic restraint (tight negative tracking, one weight doing real
work) and from things on the page actually moving because the system is actually live — not
from color noise.

## Structure

Keep v2's structural fix — it was correct and should not be undone by this palette change:

- **Command rail** — fixed, ~280px, does not scroll. Wordmark, live-status pulse, the 6
  event-emission buttons stacked vertically, and a clickable list of every investigation run.
- **Stage** — scrolls independently, shows one investigation's full detail plus the
  lineage/debt/fleet/runtime panels. Switching the rail's active investigation swaps stage
  content with a real transition — this is what makes it an app, not a page.

## Color

```
--void:        #0A0A0B   /* page background. Not #000 - Linear's own is #08090A. */
--surface:     #131315   /* card/panel background, one step up from void */
--surface-2:   #1B1B1E   /* rail background, hover surfaces */
--border:      #FFFFFF14 /* hairline, ~8% white - never a solid grey */
--border-strong: #FFFFFF29  /* ~16%, for the active/focused card only */

--text:        #F2F2F0   /* primary - near-white, not pure #FFF */
--text-soft:   #9A9A9D   /* secondary / body prose */
--text-mute:   #6B6B6E   /* tertiary / timestamps / deep labels */

--accent:      #5B7FFF   /* the ONE interactive accent - a cooler, more clinical blue than
                             Linear's indigo (#5E6AD2), chosen so this reads as Ariadne's own
                             identity rather than a copy. Used ONLY for: the live-pulse dot,
                             the active rail item's left bar, focus rings, links, and the
                             ticker's accent text. NEVER a background fill of any real size,
                             NEVER a gradient, NEVER on a verdict. */
--accent-dim:  #5B7FFF26  /* ~15%, for the active rail row's background wash only */

/* Verdicts - unchanged meaning, now the ONLY saturated colour family the page ever shows
   at real size. This matters more here than in any previous pass: against a near-void
   background, a verdict word in full saturated colour is the single most dramatic thing
   that can happen on the page - exactly the "wow" a four-second glance needs. */
--supported:    #2FBF8F
--contradicted: #FF5C3D
--inconclusive: #E8B84B
```

No second decorative hue anywhere. No gradient except the one narrow functional scrim Linear
itself uses (a media/panel fading to `--void` at its lower edge) — never a full-bleed
background gradient, never behind the hero or the verdict.

## Typography

- **Display — Space Grotesk, weight 700, tight tracking (`-0.03em` to `-0.045em` at the
  largest sizes).** The verdict word stays the dominant statement from v2
  (`clamp(4rem, 11vw, 9rem)`) — against `--void` in a fully saturated verdict colour, this is
  the moment the whole page is built around.
- **Body — Inter.** Matches Linear's own choice exactly; it is genuinely the right tool here
  (excellent at small sizes, quiet at low contrast against dark).
- **Mono — JetBrains Mono or Berkeley Mono if available.** Unchanged role: every number, id,
  hash, label. Extra tracking (`0.14em`–`0.16em`) on rail/eyebrow labels, exactly like
  Linear's own label treatment.

## Chrome

- **Hairline borders only** (`--border`, ~8% white) — never Linear's thicker glass-panel
  shadows, and never v2's heavy 2px borders. Precision reads through restraint, not weight.
- **No glassmorphism, no backdrop-blur.** This is the one piece of Linear's own system
  deliberately NOT adopted — blur-and-translucency is now itself a cliché in AI-tool UI, and
  it works against the "instrument, not app" read this product needs.
- **Radius 0–4px.** Slightly softer than v2's 0–2px is acceptable (Linear uses 6px on cards)
  but stay well short of anything that reads as a consumer app.
- **The ticker**, kept from v2 but re-skinned: a full-width band in `--surface-2`, hairline
  top/bottom borders, mono text in `--text-soft` with values in `--text` — NOT a solid
  `--accent` fill (that would make the one rationed accent color decorative, which breaks its
  whole purpose). The ticker's job is density and motion, not colour.

## Motion — precise, not decorative (Linear's actual timing values)

- Colour/text state changes: `0.1s ease`.
- Background fills, hover states: `0.4s ease-out`.
- Panel/card entrances (the rail-switch transition, stage reveal): `0.16s
  cubic-bezier(0.25, 0.46, 0.45, 0.94)` — snappy, zero overshoot, no bounce.
- The ticker scrolls continuously (linear keyframe, ~30s loop, pauses on hover).
- The live-status dot pulses — reuse the existing `.spin` semantics (means genuine in-flight
  work, nothing else).
- Stage-timeline entries reveal on scroll with a short fade+rise, staggered ~40ms.
- Honour `prefers-reduced-motion` throughout.

## Non-goals (binding)

No purple-to-cyan or multi-hue glow gradients. No glassmorphism/backdrop-blur anywhere. No
decorative illustration. No second accent colour beyond `--accent` and the three verdict
colours. No pure `#000` background (use `--void`, which is not the same thing). Nothing on
this page should be colourful or "busy" except the verdict itself and the three signal colours
it draws from — that scarcity is what makes the verdict moment land.
