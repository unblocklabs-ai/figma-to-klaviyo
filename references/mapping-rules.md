# Mapping rules: what the converter does and why

Open this when `convert` output surprises you, or when a `decisions.json` rule name needs explaining. The rules are what `scripts/ir.py` (Figma tree → bands/rows/cells) and `scripts/emit.py` (→ Klaviyo body) implement.

- *(verified live)* marks rules previously confirmed in a Klaviyo sandbox, at 600 px and real 375 px mobile. Client source designs are not distributed in this repository.
- Unmarked rules were not singled out in a live render.

## Contents

- Structure
- Rows and columns
- Type
- Buttons
- Images and backgrounds
- Spacing
- Mobile
- Footer
- TODO markers and the push gate
- Decision rules
- Known limitations
- Things Figma doesn't tell you

## Structure

| Figma | Klaviyo |
|---|---|
| Hidden or zero-opacity layer | Skipped with its whole subtree (`skipped-hidden`) |
| Each top-level child of the frame (a band; a frame, instance, or component, which an IMAGE fill turns into a section background with live text kept on top), sorted top to bottom | One `section` per band *(verified live)*. A band with a card grid or a text-on-photo layout is still one section per band or per card (see below). |
| Band fill | Section `background_color`, plus `content_color` = the same color and `content_color_type: "section"` on **every** section *(verified live)*. Without it Klaviyo paints the band color only on the outer edge and the 600 px column keeps the template content color, so colored bands look unpainted. |
| Frame background (the gutters beside the 600 px column) | `page_background` from the brand if set, else the photo-free band color with the most total height, else (every band has a photo) the last band's color *(verified live)*. Logged as `page-background`. |
| Text | `text` block: `<h1>`–`<h4>` or `<p>`; see Type |
| Frame/instance with one text, solid fill or stroke, height ≤ 80 | `button`; see Buttons |
| Photo, illustration, logo, icon (IMAGE fill, mask group, or text-free vector group) | `image` block from an exported file; see Images |
| LINE, or rectangle ≤ 2 px thick | `horizontal_rule` (vertical rules are dropped, see `vertical-divider`) |
| Frame, group, instance, component, or component set | Flattened: its children become the elements (`flatten-container`) |
| Anything else (unsupported type, unpainted shape) | A `TODO:unmapped:<id>` text block (`unknown`); resolve it with `skip` or by fixing Figma |
| Solid rectangle with nothing on it | Omitted (`solid-shape`) |

## Rows and columns

- **Multi-cell rows are their own section** *(verified live)*. Consecutive single-cell rows of a band share one section; a row with 2–4 columns gets its own section so it can carry `column_align` and `column_direction`, which are section-level settings.
- **Cells come from geometry.** Elements are grouped into rows by vertical overlap; within a row, elements that don't overlap horizontally become left-to-right cells (`columns`, medium). Elements overlapping in both axes stay in one cell in reading order (`overlap`, low).
- **Column presets** *(verified live)*. Klaviyo only offers fixed presets: 1 column; 2 columns (equal, 25/75, 33/67, 67/33, 75/25); 3 columns (equal, 25/25/50, 25/50/25, 50/25/25); 4 equal. The converter:
  1. measures each cell's width by splitting at the **middle of the gap** between neighboring cells, not by the cells' own widths;
  2. rejects any preset that would make an image column narrower than the widest image in it (min widths);
  3. picks the nearest preset.
- **More than 4 columns:** only the first four are kept (`columns-overflow`, low). Fix the Figma layout.
- **Vertical alignment:** a multi-column row whose cell centers are within 12 px gets `column_align: "middle"`, else `top`.
- **Image on the right → `rtl`** *(verified live)*. If the first cell has no image and a later one does, the section gets `column_direction: "rtl"`. Desktop order is unchanged; phones stack the image first.
- **Aligned-row merging** *(verified live)*. Two consecutive multi-cell rows with the same number of cells whose cells overlap horizontally track by track (icons above labels) become one row of multi-block cells (`row-merge`). If the pair alternates image and text lanes it stays separate (`row-merge-deferred`, medium).
- **Full-width card grouping** *(verified live)*. A solid or stroked rectangle/frame at least 60% of the band width, painted behind content, groups that content into one row per card (`card-group`, medium), each card becoming its own section:
  - quarter-turn labels (rotated 90/270) are hoisted into a full-width heading row above the card's content (`card-label-hoisted`); the rotation is not reproduced;
  - **A backdrop is not a card** *(tested)*. A shape or frame covering at least 95% of the band width and at least 90% of its height is the band's background; its elements stay in normal rows;
  - inside a card, text is split into columns with the normal x-gap rule, and images go left or right of the text group by their centre: images left of the text are one cell, the text cells next, images right of it last (`columns`, medium) *(tested)*;
  - a row in a card section is vertically centered, and card text columns are protected from being squeezed (text min width, capped at 60% of the section) while images shrink to their column.
  - The card's own fill and border are not reproduced (`card-style-dropped`, medium). Narrower cards are not grouped at all (`card-frame`, low).

## Type

- **Body role by character volume** *(verified live)*. Among text styles (family, size, weight, italic) of 22 px or less, the one with the most characters becomes the `text` role (the template default). If no style is that small, the smallest size group is used.
- **Headings by size** *(verified live)*. Styles over 22 px, largest first, become `h1`–`h4`. Styles with the same family, weight, and italic within 2 px of each other share a role. A fifth distinct size is kept as a paragraph with inline styles (`heading-overflow`, medium).
- **What a role carries:** font stack, size, weight, italic, line height as a multiplier, letter spacing, color, alignment, and the mobile size. Each text block then adds inline CSS only where it differs from its role (line height, letter spacing, color, alignment). Inline font sizes (from mixed-style runs, or forced roles) do not shrink on mobile (`inline-size`, medium).
- **Forced roles:** `overrides.roles` sets `text`/`h1`–`h4` for one text (`role-override`).
- **Fonts:** the stack comes from the brand `font_map` by Figma family name. An unmapped family gets a fallback stack and `font-unmapped` (medium). An italic the brand says is unavailable is still emitted as italic (the browser fakes it) and flagged `font-italic-unavailable` (medium).
- **Mixed styles in one text** (`mixed-text-styles`): emitted as `<span>` runs with only the differing weight, italic, color, or size.
- **Text case:** `UPPER`/`LOWER` are applied to the characters themselves.
- **Line breaks:** a hard break in Figma text becomes `<br>`, including on mobile.
- **`max-width` from the text box, single-cell rows only** *(verified live)*. In a one-column row a text narrower than the column gets an inline `max-width` (box width + 8) so Figma's line breaks are kept; it is centered with auto margins when its insets are within 12 px of equal, and pushed right when the right inset is smaller. In multi-cell rows the column preset sets the measure, and a `max-width` would persist after the columns stack, so none is written. Rotated text never gets one.
- **Text overrides:** `overrides.text` replaces the characters of a Text node (or a button's label node) and drops its style runs (`text-override`).

## Buttons

- **What is a button:** a frame or instance (not a band) with exactly one visible text descendant, height ≤ 80 px, a solid fill or stroke, and no image fill inside (`button`). Non-label artwork inside it (an icon arrow) is dropped and logged (`button-artwork-dropped`, low).
- **Fill** *(verified live)*: the Figma fill; with only a stroke (outline button) the band color (or page color) is used as the fill *(verified live)*.
- **Stroke** *(verified live)*: a stroke becomes the button border, also on filled buttons.
- **Fixed width** *(verified live)*: Klaviyo sizes a button to label plus padding. Top and bottom padding come from Figma; left and right padding are the larger of Figma's padding and `(button width − label width) / 2`, so a 200 px button stays about 200 px.
- **Also carried:** label font, size, weight, color, letter spacing (rounded to whole pixels), corner radius, and position in the column (left/center/right by offset).
- **Link:** `overrides.links[<button node id>]`, else `TODO:link:<id>`.

## Images and backgrounds

- **Node export with exact bounds, cropped to the visible band area** *(verified live)*. `assets` asks Figma for the node at 2x with `use_absolute_bounds`, then crops the render to the part inside its band (`clipped_box`). Figma renders a node's full bounds even when the band clips it; without the crop, an image overflowing its band would be stretched to the wrong size.
- **Format and matte** *(verified live)*. Photos are JPEG (quality 85), flattened onto the **band color** (else the most common band color), so a circular or masked photo doesn't get white corners. Text-free vector artwork (`vector-image`) stays PNG with alpha, and its image block gets `max_width` so it doesn't grow on mobile.
- **Display size:** the visible width, capped at the column width. Alignment from the offset within the column.
- **Alt text and links:** `overrides.alt[<id>]` (else `TODO:alt:<id>`); an image is linked only if `overrides.links[<id>]` exists.
- **Text over a photo *(verified live at 600 and 375)*.** A band with a background photo becomes **one** section with `background_url`, `background_asset_id`, `background_format: "cover"`, `background_position: "center-center"`, and all its live text and buttons as blocks on top. Section height is content plus padding, so text position inside the photo comes from block and section padding.
  - Which image is the background (`band-background-image`): an image covering at least 60% of the band and overlapping text, buttons or other images, and not painted over earlier content. One covering 99% or more wins; otherwise a single candidate is promoted when the band frame has no fill of its own. Extra candidates stay content (`background-image-ambiguous`, low). A band whose own frame fill is the photo uses that fill; if a later child image fully covers it, the child wins and the frame fill is omitted (`occluded-fill`).
  - Why it never becomes a flat image: a raw fill export (`image-fill`) is used whenever a node export would bake in live text.
- **Fill-only backgrounds are composited locally** *(verified live: composited heroes matched Figma)*. A raw fill is the source photo, so `assets` rebuilds what Figma shows: the `imageTransform` crop, scale mode (FILL, FIT, STRETCH, TILE), then SOLID overlays and linear or radial gradient overlays in paint order, flattened on the matte and cropped to the visible area (`composite-fill`, medium). Unsupported blend modes and image filters are rendered as NORMAL or ignored, with a warning in `asset_plan.json`.
- **Topmost fill wins** *(verified live)*: where fills are stacked, the visible solid color is the last (topmost) visible SOLID paint.
- **Skipped images:** an image whose visible area is empty (off the canvas) is omitted (`offcanvas-image`, low). Images in a fifth or later column are omitted (`columns-overflow`), and `skip` drops images too. `assets` plans only the images `convert` places, so these are never exported or uploaded (pass `assets --overrides`).

## Spacing

- **One owner per gap.** Each gap is either a block's `block_padding_top` or a section's `inner_padding_*`; there are no spacer blocks.
- **Between blocks:** distance in Figma, clamped at 0. A negative gap (overlap) is clamped and logged (`overlap-gap`, low), because blocks can't overlap.
- **Between rows in a section:** the row gap, given to the row above when columns are centered.
- **Section padding:** top and bottom come from the gap between the band edge and its content. Gaps between bands and before/after the frame are added to the adjacent section's padding. A skipped band adds no gap.
- **Side padding.** Auto-layout bands use Figma's padding. Absolute-position bands use the smallest inset of their elements, **capped at `brand.gutter` (default 40)** *(verified live)*. Klaviyo keeps section padding on phones, so a 97 px inset left 181 px of mobile text width and broke words. The text's own `max-width` keeps desktop line breaks. If padding would leave no width, the band edges are used (`section-width-fallback`, low).

## Mobile

Figma has no mobile design; these are the converter's choices.
- **Stacking** *(verified live)*: Klaviyo stacks columns below 480 px; `rtl` only reverses the order (see Rows and columns). The converter never uses `no-stack`.
- **Mobile sizes** *(verified live)*. Per role: `brand.mobile.<role>` if set; otherwise by desktop size: 40 px or more × `display_factor` (0.65), over 22 px × `heading_factor` (0.8), else × `body_factor` (0.9) with a floor of 16 px for body text and 12 px otherwise. So 84→55, 56→36, 28→22, 20→18. Roles carry the mobile size; inline sizes don't.
- **Verify** with `check`, which renders a real 375 px viewport.

## Footer

- `footer.mode: "standard"` (default): a compliant footer section is appended: organization name and address, `{% unsubscribe %}`, `{% manage_preferences %}`, in the body font at 11 px, centered, on the page color. Its text color is `footer.color`, else `#CCCCCC` on dark pages and `#555555` on light ones.
- `footer.mode: "none"`: no footer, and `footer-none` is logged. Insert the brand's universal footer in Klaviyo by hand; the API can't place a universal block.
- Footer links use the template's link color (`link_color`); set it so they stay visible on the page color.

## TODO markers and the push gate

- A missing brief input is never guessed. `convert` writes a marker: `TODO:link:<node id>` (button href), `TODO:alt:<node id>` (image alt), `TODO:asset:<node id>` (image not in `assets.json`; the URL is a `todo.invalid` placeholder), `TODO:unmapped:<node id>`.
- `convert` lists them in `todo.md` and gives a worksheet, `overrides.todo.json`, with an empty entry per missing link and alt.
- `validate` and `push` decode the body (HTML-escaped, URL-encoded) and refuse any marker, and any placeholder `href`: empty, `#`, `javascript:`, `localhost`, `*.example`, `*.invalid`, `*.test`, `example.com/org/net`, or no host. `validate --allow-todo` only tolerates them in drafts. `push` has no bypass; fix the input.
- `check` also reports `placeholder_links` from the rendered HTML.

## Decision rules

Every entry in `decisions.json` has `node_id`, `rule`, `detail`, `confidence`. `todo.md` lists the low ones under "Needs review". High needs no action; medium deserves a glance at `compare.png`; low: decide.

| Rule | Conf. | Meaning and what to do |
|---|---|---|
| `skipped-hidden` | high | Hidden or zero-opacity subtree skipped. Nothing to do. |
| `skipped-override` | high | Dropped by `overrides.skip`. |
| `text-override` | high | Text replaced by `overrides.text`; style runs discarded. |
| `role-override` | high | Heading level forced by `overrides.roles`; Figma style differences stay inline. |
| `flatten-container` | high | Frame/group flattened into live children. Nothing to do. |
| `button` | high | Frame recognized as a button. Check its link is filled. |
| `button-artwork-dropped` | low | An icon or shape inside the button was dropped. Accept, or restructure in Figma. |
| `mixed-text-styles` | high | Text kept as styled runs. |
| `rotated-text` | low | Rotated text kept live but not rotated. Check `compare.png`; quarter-turn labels in cards are hoisted instead. |
| `vertical-divider` | low | Vertical rule dropped; email can't render one. |
| `vector-image` | high | Text-free vector exported as a PNG with `max_width`. |
| `mask-group` | high/low | Mask group exported as one image (high). Low: the mask contains text, so it is not exported as an image; its children stay live and may need restructuring in Figma. |
| `image-fill` | high | Raw fill used because a node export would bake in live text. |
| `composite-fill` | medium | Fill stack (crop, overlay, gradient) is rebuilt by `assets`. Look at the image; read `asset_plan.json` warnings. |
| `band-background-image` | high | Image promoted to the section background, text live on top. |
| `occluded-fill` | high | A band's own image fill was hidden under a child photo; the child is the background. |
| `background-image-ambiguous` | low | Large image kept as content (another background exists, or it covers earlier content). Check the band looks right; else fix layers. |
| `offcanvas-image` | low | Image has no visible area; omitted. |
| `card-background` / `solid-shape` | high | Solid shape behind content (a card) / decorative shape with nothing on it; omitted. |
| `card-group` | medium | Full-width card: content grouped into a row and section. |
| `card-label-hoisted` | medium | Rotated card label became a heading row. |
| `card-style-dropped` | medium | Card fill and border not reproduced (known limitation). |
| `card-frame` | low | A card frame too narrow to group; its styling is not reproduced. |
| `columns` | medium | Geometry produced multiple cells. Confirm the column count. |
| `columns-overflow` | low | More than four columns; extras omitted. Restructure in Figma. |
| `overlap` | low | Two elements overlap in both axes; kept in one cell in reading order. Usually a collage; check. |
| `overlap-gap` | low | Overlap gap clamped to 0. |
| `row-merge` | high | Aligned rows merged into one (icons over labels). |
| `row-merge-deferred` | medium | Alternating image/text lanes left as separate rows. |
| `gutter-fallback` | low | Brand `gutter` was negative or not a number (library callers only; `convert` rejects it); 40 used. |
| `section-width-fallback` | low | No content width after padding; band edges used. |
| `page-background` | medium | Page color chosen; set `page_background` in the brand to pin it. |
| `font-unmapped` | medium | Family not in `font_map`; fallback stack used. Add it to the brand config. |
| `font-italic-unavailable` | medium | Italic used but the brand font has none; the browser fakes it. Report it. |
| `heading-overflow` | medium | More than four heading sizes; extras are inline paragraphs. |
| `inline-size` | medium | Inline size over 16 px won't shrink on mobile. |
| `footer-none` | high | Footer omitted by brand config; add the universal footer by hand. |
| `unknown` | low | Unsupported node; a `TODO:unmapped` block marks it. `skip` it or fix the design. |

## Known limitations

- **Card styling:** card fills and borders are not reproduced (`card-style-dropped`, `card-frame`). Content sits on the band color. Report it as a known difference.
- **Image strokes:** a stroke on a photo (for example a white frame) is not in the export and is lost.
- **Overlapping collages** beyond cards come out as reading-order cells (`overlap`), not free positioning.
- **Hard line breaks** from Figma are mirrored on mobile and can rag; remove them in Figma.
- **Universal footer:** the brand's own universal footer section is untested through the API; use `standard` or `none` and say which.
- **Fonts:** missing weights or italics render as fallback or faked; clients without web fonts show the fallback stack.
- **Button labels** are HTML-escaped; whether Klaviyo shows an `&` or `<` in a label literally has not been verified live. Check `compare.png` if a label has one.
- **Rotated text** outside cards loses its rotation.

## Things Figma doesn't tell you

| Missing from Figma | Where it comes from |
|---|---|
| Links and UTMs | The brief (placeholders are a blocker) |
| Alt text | Written by you from the image; the brief may set the tone |
| Mobile sizes | Brand `mobile` config, else the defaults above |
| Footer | Brand config `footer`; universal footer by hand |
| Subject line and preview text | The canvas metadata box beside the frame. They go on the campaign message (see the `klaviyo` skill), not the template. |
