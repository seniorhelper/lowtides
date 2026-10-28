# Lowtides brand spec

**Lowtides.ai** is an AI spend governor: budgets, limits and kill switches for AI agents, with spending funded and settled through PayPal.

## Logo

The mark is a 6×6 dot grid. The **amber top row is the spend limit**. A row of faint dots always sits under it as a safety gap. The **sea-glass dots are spend**, sitting low like a low tide.

| Use | File |
|---|---|
| Main mark (light backgrounds) | `svg/lowtides-mark.svg` |
| Main mark (dark backgrounds) | `svg/lowtides-mark-on-dark.svg` |
| Animated mark: site header, loading state, video intro | `svg/lowtides-mark-animated.svg`, `svg/lowtides-mark-animated-on-dark.svg` |
| Small 4×4 mark for 16–32 px | `svg/lowtides-mark-small.svg`, `svg/lowtides-mark-small-on-dark.svg` |
| Favicon (dark tile, works on any tab color) | `favicon.ico`, `svg/favicon.svg`, `png/favicon-16/32/48/180/512.png` |
| Lockup, "lowtides" | `svg/lowtides-lockup-light.svg`, `svg/lowtides-lockup-dark.svg`, `png/lockup-light@2x.png`, `png/lockup-dark@2x.png` |
| Lockup, "lowtides.ai" | `svg/lowtides-ai-lockup-light.svg`, `svg/lowtides-ai-lockup-dark.svg`, `png/lockup-ai-light@2x.png`, `png/lockup-ai-dark@2x.png` |
| GitHub social preview (1280×640) | `png/social-preview-1280x640.png` |
| Devpost gallery thumbnail (1500×1000, 3:2) | `png/devpost-thumbnail-1500x1000.png` |
| Spend-limit screen icon (in-app only, not the logo) | `svg/icon-spend-limit.svg` (structure uses `currentColor`) |
| Large transparent mark | `png/mark-512.png` |

**Rules**
- The wordmark is always lowercase: `lowtides` or `lowtides.ai`. In running text write "Lowtides".
- The SVG lockups load Sora from Google Fonts. In a README or anywhere fonts can't load (GitHub strips them), use the PNG lockups.
- Leave clear space around the mark of at least one dot-row (1/6 of the mark's height).
- Never recolor the limit row to green, and never add lit dots in the top two rows. The gap under the limit is the idea.
- Below 32 px, use the 4×4 small mark.
- Never place the PayPal logo inside our logo. When showing PayPal, use their official assets, apart from our mark, following PayPal's brand guidelines.

**Animation:** in each column the sea-glass dots rise and fall in a 3.6 s loop, offset per column, so a wave runs across the grid. They never reach the row under the limit. With `prefers-reduced-motion`, the SVG shows the static logo. To keep the animation running, embed it with `<img>` or inline it. For the video intro, record it on the dark background for about 4 seconds.

## Colors (palette A, Sea Glass)

| Token | Light UI | Dark UI | Role |
|---|---|---|---|
| `--lt-bg` | `#F4F8F7` | `#0A1B1E` | Page background |
| `--lt-ink` | `#0D2A2E` | `#E6F4F1` | Text, structure |
| `--lt-muted` | `#4E6468` | `#9DB5B1` | Secondary text |
| `--lt-tide` | `#1FB89A` | `#3FD6B5` | Spend, healthy state, accent |
| `--lt-limit` | `#E39A2D` | `#F2B24D` | Limits, caps, warnings |
| `--lt-kill` | `#D9483B` | `#FF7A66` | Kill switch, over-limit, errors (UI only, not in the logo) |

Accessibility: on light backgrounds, `--lt-tide` and `--lt-limit` are for graphics and large text only (under 4.5:1 for small text). For small accent-colored text on light, use `#0F7F6A` (tide) or `#9A5B00` (limit). On dark, all tokens pass 4.5:1 against `--lt-bg`.

Palettes B (Moonlit Indigo) and C (Low Water) from the options sheet can be swapped in later. Only the hex values change.

```css
:root{--lt-bg:#F4F8F7;--lt-ink:#0D2A2E;--lt-muted:#4E6468;--lt-tide:#1FB89A;--lt-limit:#E39A2D;--lt-kill:#D9483B}
[data-theme="dark"]{--lt-bg:#0A1B1E;--lt-ink:#E6F4F1;--lt-muted:#9DB5B1;--lt-tide:#3FD6B5;--lt-limit:#F2B24D;--lt-kill:#FF7A66}
```

## Type (all free Google Fonts, OFL licensed)

| Role | Font | Use |
|---|---|---|
| Wordmark and headings | **Sora** 600 (500 for ".ai"), tracking −0.04em on the wordmark, −0.02em on headings | Logo, page titles, video titles |
| UI and body | **Instrument Sans** 400/500/600 | Dashboard, README body, captions |
| Numbers, money, labels | **JetBrains Mono** 500, tabular numbers, small caps-style labels at +0.08em | Dollar amounts, budgets, logs |

```html
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=Instrument+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
```

## Taglines

1. **Keep AI spend at low tide.** (primary)
2. Your agents spend. You set the tide line.
3. Budgets, limits and kill switches for AI agents.
4. Set the limit. Let your agents work.
5. Calm spending for busy agents.

## Voice

Calm, plain and exact. Lead with control and safety, use real numbers, and skip hype. "Governor" is fine to describe the product.
