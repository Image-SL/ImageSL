# ImageSL design system

The values here are not invented. They are the points on which the published
design systems of large software companies agree, and where they disagree the
choice is stated with the reason.

**Method, stated honestly:** this is drawn from the *published specifications* of
these systems — Apple Human Interface Guidelines, IBM Carbon, Shopify Polaris,
GitHub Primer, Salesforce Lightning, Material Design, Atlassian, Adobe Spectrum
— together with the observable conventions of Stripe, Linear, Vercel, Figma,
Notion, Superhuman and Raycast. It is not a scrape of live CSS. Where a number
below is a convention rather than a documented rule, it says so.

---

## 1. Spacing — a 4px base, used as a scale not a free choice

Every one of Carbon, Polaris, Primer, Spectrum and Material builds spacing on a
**4px base**, exposed as a small set of steps rather than arbitrary numbers.
Material uses 8dp with 4dp permitted; Carbon's steps are 2, 4, 8, 12, 16, 24,
32, 40, 48, 64; Polaris is 4-based.

```
--s1 4   --s2 8   --s3 12  --s4 16  --s5 20
--s6 24  --s8 32  --s10 40 --s12 48 --s16 64
```

The rule that matters more than the numbers: **a component may only use steps
from this scale.** Inconsistent rhythm is the single most reliable sign that an
interface was assembled rather than designed, and it is invisible to the person
making each individual choice.

## 2. Type — a modular scale, ratio ≈ 1.20

Enterprise systems cluster on a **minor-third (1.2) to major-third (1.25)**
ratio for UI, not the 1.414/1.618 ratios used in editorial layout — large ratios
produce headings that dwarf body copy and waste vertical space in a working
tool.

| Step | Size | Use |
| --- | --- | --- |
| micro | 11.5 | metric labels |
| small | 12.5 | captions, meta |
| body | 13.5 | UI text, secondary |
| base | 15 | body copy |
| lead | 17 | card titles |
| title | 20 | section headings |
| display | 28 | page headings |
| hero | 34–50 | one per page, marketing only |

**Line height**: 1.5 for body, 1.2 for headings. This is unanimous across
Carbon, Polaris, Primer and Material. Tight leading on long text is the most
common readability failure in "minimal" designs.

**Measure**: body text is capped at **60–75 characters**. Every system states
this; the number comes from typographic research, not taste. Our prose is capped
at `--measure: 68ch`.

**Weight**: two weights carry almost everything — regular (400) and medium
(500). Apple, Stripe and Linear all use semibold sparingly and thin essentially
never below 40px. Thin weights at heading size read as weak, which is the exact
mistake this codebase made and corrected.

## 3. Colour — one accent, a neutral ramp, and nothing else

The consistent pattern is a **9–10 step neutral ramp** plus **one brand accent**
plus semantic status colours. Stripe, Linear, Vercel and Apple all restrict
accent use to a single primary action per view.

- Canvas `#f5f5f7`, panels `#ffffff`. Apple's own inversion: content is brighter
  than the page, so panels separate themselves without borders.
- Text `#1d1d1f` / `#515154` / `#6e6e73`. Warm near-blacks, never pure `#000`.
  Pure black on white is harsh and no major system uses it for body text.
- **Contrast floor 4.5:1** for normal text, 3:1 for large. Non-negotiable and
  machine-checked in this repo.

## 4. Radius — objects, not form fields

Convention rather than published rule, and the range is narrow: Stripe and
Linear ~8px, Vercel 6–8px, Apple 10–18px for panels and fully rounded for
segmented controls and pills.

```
--radius-sm 8    inputs, chips
--radius   12    cards, panels
--radius-lg 18   large containers
--radius-pill    segmented controls, buttons
```

Below about 6px a container reads as a form field. That is why the earlier 3px
version of this interface looked like a data-entry screen.

## 5. Elevation — two levels, soft and wide

Material's 24 elevations are widely regarded as excessive; Polaris, Primer and
Apple all use two or three. Shadows are **soft, wide and low-opacity**; a tight
dark drop shadow is the clearest tell of an amateur interface.

## 6. Layout

- **Container 1120px.** Stripe, Linear, Vercel and Notion all land between 1000
  and 1280. Wider than ~1280 breaks the measure rule for text.
- **12-column grid**, because it divides by 2, 3, 4 and 6.
- **Optical centring**: a vertically centred block is placed slightly *above*
  true centre. Perfect mathematical centring reads as low.
- **Touch/click target ≥ 44px** (Apple HIG) / 48dp (Material). Ours: 44px.

## 7. Motion

150–250ms for UI feedback, ease-out. Anything above ~300ms feels slow in a
tool used repeatedly. `prefers-reduced-motion` is honoured.

---

## What this is not

A design system does not make a product good; it removes a class of small
inconsistencies that add up to looking unconsidered. The decisions that actually
determine whether this application is pleasant — what the upload screen asks
for, how a result is presented, what happens when a slide fails — are product
decisions and are argued in the code where they are made.
