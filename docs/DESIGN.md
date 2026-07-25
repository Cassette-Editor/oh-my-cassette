---
name: Cassette
description: A browser-based video editor with an AI co-editor, tuned like a colorist's grading suite.
colors:
  amber-key: "#f49325"
  coral-signal: "#f94639"
  coral-glow: "#fa665c"
  peach-glow: "#fd916d"
  sand-glow: "#febe85"
  lemon-glow: "#fbe774"
  grade-black: "#07080b"
  panel: "#101216"
  surface: "#14151a"
  elevated: "#191a1f"
  hairline: "#2a2b32"
  ink: "#f7f7f8"
  ink-secondary: "#b3b5bd"
  ink-muted: "#888a96"
  ink-faint: "#61626b"
  destructive: "#dc2828"
  success: "#20c55d"
  warning: "#f59f0a"
  info: "#3c83f6"
  connected-cyan: "#17cce8"
typography:
  title:
    fontFamily: "Inter, system-ui, -apple-system, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  heading:
    fontFamily: "Inter, system-ui, -apple-system, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Inter, system-ui, -apple-system, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, system-ui, -apple-system, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: "normal"
  numeric:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: "normal"
rounded:
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  full: "9999px"
spacing:
  px: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  "2xl": "32px"
components:
  button-default:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.grade-black}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-default-hover:
    backgroundColor: "{colors.ink-secondary}"
    textColor: "{colors.grade-black}"
  button-outline:
    backgroundColor: "{colors.grade-black}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-secondary:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-ghost:
    backgroundColor: "{colors.grade-black}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  input-default:
    backgroundColor: "{colors.grade-black}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
    height: "40px"
  panel-surface:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "16px"
  dialog-surface:
    backgroundColor: "{colors.grade-black}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "24px"
---

# Design System: Cassette

## 1. Overview

**Creative North Star: "The Color Suite"**

Cassette is a colorist's grading suite rendered in the browser. The room is dark on purpose: a near-black background (`#07080b`, a cool blue-black) so that the footage in the preview is the brightest thing on screen and the editor's eye calibrates to the image, not the chrome. Everything around the picture is cool, low-chroma instrumentation. The one warm light in the room is the amber key (`#f49325`), and like a real key light it is used sparingly: it marks the playhead, the focus ring, the current selection, the live link. The AI co-editor's presence is the single ambient glow, a slow conic ring that cycles coral through lemon around whatever surface it is working in. Nothing else competes with the image.

This system serves a professional task, so it earns density that a marketing surface could not. Panels carry many labels, timelines carry many clips, inspectors carry many fields, and that is correct: editors moving fast want information in front of them, not hidden behind progressive reveals. Density is managed through hierarchy and a cool tonal ramp, never through decoration. Clip colors are deliberately desaturated by type (a 25%-saturation cinematic palette) so that real thumbnails and real footage read as the saturated content and the timeline reads as scaffolding.

What this system explicitly rejects: the blue-and-purple palette of competing editors, and the cold, clinical feel that comes with it. Cassette's differentiation is warmth carried by a single amber accent and a coral-to-lemon AI spectrum, never by tinting the whole surface. It also rejects spectacle: the AI is a co-pilot, so its motion is ambient awareness, never a flashy interruption. If a screen looks like it is performing for the user instead of getting out of the way, it is wrong.

**Key Characteristics:**
- Dark-first grading-room surface (`#07080b`), cool-gray instrumentation, warm accent reserved for state.
- Information density managed by hierarchy and a 13-step cool-neutral ramp, not by hiding controls.
- One warm amber accent (`#f49325`) for interactive state; coral→peach→sand→lemon reserved for AI presence.
- Desaturated, type-coded timeline clips so real footage is the only saturated content.
- Restrained motion: 100–300ms state transitions, no orchestrated page loads, full reduced-motion fallbacks.

## 2. Colors

A dark grading-room palette: a cool blue-black surface, a 13-step cool-neutral ramp for instrumentation, one warm amber accent for state, and a coral-to-lemon warm spectrum reserved for the AI. All tokens are authored in HSL channel format (`H S% L%`) in `src/index.css`; the hex values below and in the frontmatter are faithful sRGB equivalents.

### Primary
- **Amber Key** (`#f49325`, `32 90% 55%`): The single interactive accent. The `--primary` token resolves to this warm amber (`--primary-400`), and it drives the focus ring, timeline playhead, current selection outline, active links, keyframe diamonds, and the brand mark. It is the warm key light of the suite: present, but never the whole room.
- **Coral Signal** (`#f94639`, `4 94% 60%`): The brand identity red (`--primary-500`). Used as the brand-hover state and as the leading color of the AI ring spectrum. Not an everyday UI accent; it carries identity, not interaction.

### Secondary
The AI spectrum. These four warm hues exist only to render the agent ring and aurora glow; they are never general-purpose UI colors.
- **Coral Glow** (`#fa665c`, `4 94% 67%`): Ring spectrum start/end.
- **Peach Glow** (`#fd916d`, `15 97% 71%`): Ring spectrum.
- **Sand Glow** (`#febe85`, `28 99% 76%`): Ring spectrum.
- **Lemon Glow** (`#fbe774`, `51 95% 72%`): Ring spectrum apex.

### Tertiary
Semantic state colors, used only on their states. They are saturated because they must read against the dark surface.
- **Destructive** (`#dc2828`, `0 72% 51%`): Delete actions, error states.
- **Success** (`#20c55d`, `142 72% 45%`): Confirmed/complete states.
- **Warning** (`#f59f0a`, `38 92% 50%`): Caution states. Note its hue sits next to the amber accent; never place warning and the amber accent adjacent without a label distinguishing them.
- **Info** (`#3c83f6`, `217 91% 60%`): Informational callouts.
- **Connected Cyan** (`#17cce8`, `188 82% 50%`): The "connected/linked" relationship indicator in the timeline (linked audio, attachment lines). The one cool accent permitted alongside amber.

### Neutral
A 13-step cool-gray ramp (`--neutral-50` through `--neutral-975`), hue ~222–234. Cool by design so the warm accent and warm footage read as warm by contrast.
- **Grade Black** (`#07080b`, `--neutral-975`): The room. App background, input fields, dialog surfaces, the darkest stop.
- **Panel** (`#101216`, `--neutral-900`): Editor panels and sidebars, one step up from the room.
- **Surface** (`#14151a`, `--neutral-850`): Cards, toolbars, the timeline ruler.
- **Elevated** (`#191a1f`, `--neutral-800`): Muted surfaces, popover and secondary-button base.
- **Hairline** (`#2a2b32`, `230 8% 18%`): The default border. One pixel, low contrast, structural.
- **Ink** (`#f7f7f8`, `--neutral-50`): Primary text and the default button fill.
- **Ink Secondary** (`#b3b5bd`, `--neutral-200`): Secondary text, secondary-button labels.
- **Ink Muted** (`#888a96`, `--neutral-300`): Muted/placeholder text. This is the floor for readable text; it clears 4.5:1 on grade-black. Do not go lighter-gray than this for any text a user must read.
- **Ink Faint** (`#61626b`, `--neutral-400`): Disabled text, decorative captions, lane labels. Decorative only; never body copy.

### Named Rules
**The Reserved Accent Rule.** The amber key (`#f49325`) is forbidden as decoration. It appears only where it signals interactive state: focus, selection, playhead, active link, brand mark. If the amber is filling a button, a card header, or a static panel, it is wrong, repaint it with the neutral ramp. Its rarity is what makes it read as "this is live."

**The Desaturated Stage Rule.** Timeline clip surfaces are capped at ~25% saturation (`--editor-clip` family). The footage thumbnails and the preview are the only fully saturated content on screen. If the timeline starts competing with the picture for color attention, lower the clip chroma, never raise it.

**The Cool-Room Rule.** Neutrals stay cool (hue 222–234). Never warm the gray ramp toward beige or "to feel cozy", the warmth in this product comes from the amber accent and the AI spectrum against a cool room, not from the surface.

## 3. Typography

**UI Font:** Inter (with `system-ui, -apple-system, sans-serif`)
**Numeric / Label Font:** JetBrains Mono (with `ui-monospace, monospace`)
*Instrument Serif is loaded in the global font import but is intentionally not a token; reserve it for marketing/home surfaces, never for editor chrome.*

**Character:** One humanist sans carries the entire interface: headings, labels, body, data. Hierarchy comes from weight (300–700) and a fixed rem scale, not from a second display face. JetBrains Mono appears only where digits must align and stop shifting: timecodes, frame counts, coordinate fields, deck-style labels. The pairing is functional, not decorative, sans for language, mono for numbers.

### Hierarchy
A fixed rem scale (product UI, consistent DPI; never fluid `clamp()` for chrome). Ratio is a tight ~1.1–1.15 between steps because there are many type elements on screen and exaggerated contrast would read as noise.
- **Title** (Inter 600, 1.25rem / 20px, line-height 1.25): Dialog titles, primary panel headings. The largest type in the editor.
- **Heading** (Inter 600, 1.125rem / 18px, line-height 1.25): Section headings within panels and inspectors.
- **Body** (Inter 400, 0.875rem / 14px, line-height 1.5): Default UI text and prose. Cap prose blocks (chat, descriptions) at 65–75ch; dense data rows may run wider.
- **Label** (Inter 500, 0.75rem / 12px): Control labels, field names, badges, tabs.
- **Micro** (Inter 400/500, 0.625rem / 10px and 0.5625rem / 9px): Clip accent-bar labels and the densest timeline annotations only. Never for anything the user reads as a sentence.
- **Numeric** (JetBrains Mono 500, 0.75rem / 12px): Timecodes, frame numbers, transform/coordinate values, keyframe readouts.

### Named Rules
**The Mono-for-Numbers Rule.** Any value that changes as the user scrubs, drags, or types, timecode, frame, X/Y, scale, rotation, duration, is set in JetBrains Mono so it does not reflow as digits change. Language is Inter; measurements are mono.

**The No-Display-Face Rule.** Editor chrome uses Inter only. Display serifs and decorative faces are forbidden in labels, buttons, and data; if a heading wants to feel bigger, add weight or size within Inter, do not change the family.

## 4. Elevation

Cassette is tonal-first, shadow-second. Depth is conveyed primarily by stepping up the cool-neutral ramp, the room is `grade-black` (`#07080b`), panels sit at `panel` (`#101216`), cards and toolbars at `surface` (`#14151a`), popovers at `elevated` (`#191a1f`), each separated by a 1px `hairline` (`#2a2b32`) rather than a drop shadow. Shadows are reserved for genuinely floating layers (dialogs, popovers, dropdowns) and are kept soft and dark to match a dim room. The signature "elevation" is not a shadow at all: it is the agent ring's colored glow, which is light, not shadow.

### Shadow Vocabulary
- **Panel** (`box-shadow: 0 2px 8px hsl(0 0% 0% / 0.3), 0 0 1px hsl(0 0% 0% / 0.15)`): The resting elevation for floating panels; barely-there, just enough to lift off the room.
- **sm / md / lg / xl** (`--shadow-sm` … `--shadow-xl`, e.g. `lg` = `0 10px 15px -3px hsl(0 0% 0% / 0.3), 0 4px 6px -4px hsl(0 0% 0% / 0.25)`): Escalating elevation for popovers, dropdowns, and dialogs. Opacity climbs with height; blur stays soft.
- **Glow** (`box-shadow: 0 0 6px hsl(var(--editor-glow) / 0.08)`): A faint amber bloom around live/active editor elements. Not a drop shadow, an emission.

### Named Rules
**The Tonal-Step Rule.** Two adjacent surfaces are separated by one step on the neutral ramp plus a 1px hairline, not by a shadow. Shadows are only for layers that actually float above the page. If a flat panel has a drop shadow, delete it.

**The Light-Not-Shadow Rule.** "Active" and "the AI is here" are expressed with emitted light (amber glow, the conic ring), never with heavier shadow. Shadow says "this floats"; light says "this is live."

## 5. Components

The vocabulary is precise and restrained: 8px radii, hairline borders, the amber accent admitted only on state, and 100–200ms transitions. Every interactive surface ships default, hover, focus-visible, active, and disabled. The tool should disappear into the footage.

### Buttons
- **Shape:** Gently rounded (8px, `--radius-md`); icon buttons are square (40×40). Transitions run `duration-fast` (100ms) on color, background, border, and shadow.
- **Default:** Solid ink fill, dark text (`bg-foreground text-background`, `#f7f7f8` on `#07080b`), 40px tall, `8px 16px` padding. The default action is the highest-contrast element, but it is white-on-dark, not amber. The accent is reserved.
- **Hover / Focus / Active:** Hover drops the ink fill to ~90% (`#b3b5bd`-range); active to ~80%. Focus-visible draws a 2px amber ring (`--ring`, `#f49325`) with a 2px offset against the surface.
- **Outline:** 1px `input` border on the grade-black surface; hover fills with the `accent` neutral. The standard secondary action.
- **Secondary / Ghost:** Secondary uses the `elevated` fill (`#191a1f`) with secondary ink. Ghost is transparent until hover, then takes the accent neutral. Use ghost for icon and toolbar controls.
- **Destructive:** Solid destructive red (`#dc2828`) with ink text; reserved for delete/irreversible actions.

### Inputs / Fields
- **Style:** Grade-black fill (`#07080b`), 1px `input` border, 8px radius, 40px tall, body type. Placeholder uses `ink-muted` (`#888a96`), which clears 4.5:1.
- **Hover:** Border brightens to `neutral-500`.
- **Focus:** 2px amber ring with 2px offset; border-color and box-shadow transition at `duration-fast`.
- **Disabled:** `cursor-not-allowed`, 50% opacity.

### Panels / Cards / Containers
- **Corner Style:** 8px (`--radius-md`); dialogs step up to 12px (`--radius-lg`).
- **Background:** Stepped from the neutral ramp by role (`panel` → `surface` → `elevated`), separated by 1px hairlines.
- **Shadow Strategy:** Flat at rest (tonal separation only). Floating panels may take `--shadow-panel`; see Elevation.
- **Border:** Default 1px `hairline` (`#2a2b32`).
- **Internal Padding:** 16px for panels, 24px for dialogs; dense rows use 8–12px.

### Dialogs
- **Overlay:** `bg-black/80` full-bleed at z-50.
- **Content:** Grade-black surface, 1px border, 12px radius, `shadow-lg`, 24px padding, centered, max-width `lg`. Close affordance top-right at 70% opacity, full on hover, amber focus ring.

### Timeline Clip (signature)
The core editing primitive, styled after a DaVinci-Resolve clip. A desaturated, type-colored body (≤25% saturation) with a thin saturated **accent stripe** along the top edge that encodes clip type: video `#c39322`-range warm, audio green (`#30a661`), text magenta, image amber, motion-graphic violet (`#8033cc`). Selection draws a brighter type-colored outline rather than the global amber, so multiple selected types stay legible. Linked audio and attachment relationships are drawn in connected-cyan (`#17cce8`). Clip label text is 10px and switches between light/dark for contrast against the clip body.

### Agent Ring (signature)
The AI's visual presence. A rotating conic-gradient border (`coral → peach → sand → lemon → sand → peach → coral`) masked to the edge of whatever surface the agent is working in, with an outer halo at lower opacity and a soft 1.5px blur. It rotates over `--agent-ring-speed` (4s) and fades in over 300ms. This is the only place the coral spectrum appears, and the only "look at me" motion in the product. It conveys ambient awareness; it never blocks or interrupts. Honors `prefers-reduced-motion` (animation disabled, ring shown static).

## 6. Do's and Don'ts

### Do:
- **Do** keep the room dark: app surface stays `grade-black` (`#07080b`) and panels step up the cool-neutral ramp. Let the preview be the brightest, most saturated thing on screen.
- **Do** reserve the amber accent (`#f49325`) for interactive state only: focus ring, playhead, selection, active link, brand mark. Per **The Reserved Accent Rule**, rarity is the point.
- **Do** set every changing measurement (timecode, frame, X/Y, scale, duration) in JetBrains Mono so digits don't reflow.
- **Do** separate adjacent surfaces with one tonal step plus a 1px hairline (`#2a2b32`); reach for shadow only when a layer truly floats.
- **Do** keep clip bodies desaturated (≤25% saturation) and encode clip type through the thin top accent stripe and the type-colored selection outline.
- **Do** ship default, hover, focus-visible, active, and disabled for every interactive component; focus-visible is always the 2px amber ring with 2px offset.
- **Do** keep motion at 100–300ms state transitions, and provide a `prefers-reduced-motion` fallback for the agent ring, aurora glow, and every animated affordance.
- **Do** keep readable text at `ink-muted` (`#888a96`) or lighter; it is the contrast floor on grade-black.

### Don't:
- **Don't** drift toward the blue-and-purple palette of competing editors, or let the interface feel cold and clinical. Cassette's warmth is the amber accent and coral spectrum against a cool room, not a tinted surface. (PRODUCT.md anti-reference.)
- **Don't** turn the AI into spectacle. The agent ring is ambient awareness; no flashy interruptions, no full-screen takeovers, no bouncing or elastic motion.
- **Don't** use the amber accent as decoration, a button fill, a header bar, a static panel tint. If it isn't signalling live state, it's wrong.
- **Don't** warm the neutral ramp toward beige/cream/sand "to feel cozy." The grays stay cool (hue 222–234). Warm-neutral body surfaces are forbidden.
- **Don't** introduce a display or serif face into editor chrome. Inter only for UI; add weight or size, never a new family. Instrument Serif is for marketing surfaces, not the editor.
- **Don't** raise timeline clip saturation to make clips "pop"; that steals attention from the footage. Lower chroma instead.
- **Don't** use `border-left`/`border-right` greater than 1px as a colored accent stripe on cards, list items, or callouts. The only intentional accent stripe in this system is the clip's full-width top bar.
- **Don't** add drop shadows to flat resting panels; use tonal stepping. Shadow means "floating," not "important."
- **Don't** drop readable text below `ink-muted` (`#888a96`) on the dark surface, and never set placeholder text in `ink-faint`.
- **Don't** use display `clamp()` sizing for editor chrome; the rem scale is fixed because users view at consistent DPI.
