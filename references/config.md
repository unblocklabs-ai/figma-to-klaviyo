# Config and data files

Everything `convert`, `fonts`, `assets`, and `upload` read or write. Unknown keys in a brand or overrides file are rejected with an error, so typos don't pass silently. All node ids are Figma ids such as `1:2`.

## Brand config (`--brand`)

One JSON object per brand. The brand's own skill keeps the real copy; a generic example appears below. Every key is optional.

| Key | Meaning |
|---|---|
| `font_map` | Keyed by the **Figma** family name (`"Brand Serif VF"`). Each value: `stack` (CSS font-family used in the template, with web-safe fallbacks), `klaviyo_family` (the name as registered in Klaviyo; `fonts` compares this against the account, defaults to the Figma name), `italic` (set `false` if the brand font has no italic: the italic is still emitted, but `font-italic-unavailable` is logged). Variable-font names map to the registered family here. |
| `fallback_stacks` | `{"serif": "...", "sans": "..."}`. Used for a family missing from `font_map` (logged `font-unmapped`). Built-in defaults: Georgia/Times serif, Helvetica/Arial sans. Use an explicit `font_map` entry when a font needs a particular fallback. |
| `mobile` | Mobile font sizes. Exact per role: `h1`, `h2`, `h3`, `h4`, `text` (pixels). Otherwise factors applied to desktop size: `display_factor` (40 px and up, default 0.65), `heading_factor` (over 22 px, default 0.8), `body_factor` (default 0.9, floor 16 px for body text, 12 otherwise). `{}` uses the defaults. |
| `link_color` | Template link color (default `#000000`). Footer links inherit it, so pick one visible on the page color. |
| `page_background` | Forces the page (outside-the-column) color. Without it the converter picks the photo-free band color with the most total height, else the last band's color. |
| `footer` | `mode`: `"standard"` (default; appends the compliant footer) or `"none"` (no footer; add the brand's universal footer by hand). `color`: footer text color (default `#CCCCCC` on dark pages, `#555555` on light). |
| `gutter` | Non-negative number of pixels (default 40); `convert` rejects anything else with an error, while library callers get a fallback to 40 with a `gutter-fallback` decision. The cap for side padding in free-position bands, so mobile text isn't squeezed. Also caps card-section side padding. |

## Overrides (`--overrides`)

Per-email JSON. `links`, `alt`, `text`, and `roles` must be objects of strings (`roles` values: `text`, `h1` to `h4`) and `skip` a list of strings; `convert` and `assets` exit with a message otherwise. `convert` writes `overrides.todo.json`, a copy with `links` and `alt` entries set to `""` for everything missing; fill it and pass it back. An empty string counts as still missing.

| Key | Shape | Meaning |
|---|---|---|
| `links` | `{node id: url}` | Href for a button (its frame id) or an image (its node id, optional). Must not be a placeholder. |
| `alt` | `{node id: text}` | Alt text for an image node id. Describe what the image shows. |
| `text` | `{text node id: string}` | Replaces that text's characters (use the label's text node id for a button). Style runs are dropped. |
| `roles` | `{text node id: "text" \| "h1".."h4"}` | Forces the paragraph or heading role for one text. |
| `skip` | `[node id, ...]` | Drops a band, an element (a button by its frame id), or a band background image. Skipping a button's label text id drops the whole button, not just its text. A skipped band adds no gap. Must be a list of strings. Pass the same file to `assets --overrides` so skipped images are not exported. |

Fill `links` with approved destinations from the brief and `alt` with descriptions of the actual images. Placeholder links (`example.invalid` style) are refused by `push`; never ship them.

## `asset_plan.json` (written by `assets`)

A list, one entry per unique image node that `convert` will place (skipped, off-canvas, and fifth-and-later column images are excluded; pass `--overrides` to match `convert`), backgrounds first within each band, then content in reading order. Files are written beside it.

| Field | Meaning |
|---|---|
| `node_id` | Figma node id; the key used by `assets.json` |
| `role` | `background` (section background) or `content` |
| `export` | `node` (Figma renders the node, then it is cropped) or `fill` (raw fill composited locally) |
| `box`, `clipped_box` | `{x, y, w, h}` in frame coordinates: full bounds, and the visible part inside the band (`null` if nothing is clipped) |
| `paints` | For `fill` exports, the ordered paint stack (IMAGE, SOLID, gradients); `[]` otherwise |
| `name_hint` | The Figma layer name. A hint only; never alt text |
| `filename`, `format` | File beside the plan; `png` only for text-free vector images, else `jpg` |
| `size` | Expected pixel size: visible box × scale (`--scale`, default 2) |
| `matte` | Hex color JPEGs are flattened onto: the band color, else the most common band color |
| `warnings` | Set by `assets`: unsupported paint features in a composite. Kept across `--only` re-runs. |

## `assets.json` (written by `upload --plan`, read by `convert --assets`)

An object keyed by node id, stored beside the plan:

```json
{"1:3": {"id": "4xYz123", "url": "https://<klaviyo image host>/<path>/image.jpg", "sha256": "…", "slug": "<klaviyo slug>"}}
```

- `id` and `url` are required by `convert`; a node missing either (or absent) becomes `TODO:asset:<id>`. `sha256` and `slug` together make `upload --plan` skip unchanged files: a changed file, a different `--slug` (another Klaviyo account), or an entry without a `slug` (older files) is uploaded again, so sandbox ids never leak into a production template.
- Loose files uploaded with `--image NAME=PATH --out DIR` go to `DIR/uploads.json` (`{name: {id, url}}`) and are not read by `convert`.
- Uploads are hidden in Klaviyo's library by default; `--visible` shows them.

## Reference brand config

A generic example. Replace font names with those in your own Figma design and Klaviyo account:

```json
{
  "font_map": {
    "Brand Serif VF": {"stack": "'Brand Serif', Georgia, 'Times New Roman', serif", "klaviyo_family": "Brand Serif", "italic": false},
    "Arial": {"stack": "Arial, Helvetica, sans-serif", "klaviyo_family": "Arial", "italic": true}
  },
  "link_color": "#000000",
  "footer": {"mode": "standard"},
  "mobile": {}
}
```

Note how the variable-font names ("... VF") map to the registered Klaviyo family, and how `italic: false` marks a family with no registered italic.
