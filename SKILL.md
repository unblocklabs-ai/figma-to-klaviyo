---
name: figma-to-klaviyo
description: Convert an approved Figma email frame into a native, fully editable Klaviyo drag-and-drop template with the f2k converter (fetch, convert, assets, push, check), using live text, real buttons, and images only for photography, illustration, logos, and icons. Use whenever an email designed in Figma needs to go into Klaviyo ("build this Figma email in Klaviyo", "move the approved email into Klaviyo", "make the Klaviyo template for this design"), and when a Klaviyo template has to catch up with changes to its Figma design. Load it together with `klaviyo`, `email-design-figma`, and the brand's skill.
---

# Figma → Klaviyo

**Everything a reader can read stays live.**
- Headlines, body copy, prices, CTAs, and footers become Klaviyo text and button blocks, never pictures of text.
- Images carry only photography, illustration, logos, and icons. No image slices, ever, even if the brand's past emails were built that way.
- The converter enforces this: text over a photo becomes a section background image with live text on top. If something can't be rebuilt live, stop and flag it with options. Never bake it into an image.

Ownership:
- **`email-design-figma`** owns the design and its QA. The frame you start from must already have passed it.
- **`klaviyo`** owns Loggie calls, account checks, approvals, and Klaviyo's API rules. Load it too.
- **The brand's skill** supplies the brand config JSON (see [references/config.md](references/config.md)), font names as Klaviyo registers them, and mobile sizes.

## Inputs

- The approved frame: Figma file key and frame node id.
- Loggie slugs: a `figma` integration (read-only is enough) and the brand's `klaviyo` integration. Your TOOLS.md lists both.
- The brand config JSON. Your brand skill keeps its own copy; [references/config.md](references/config.md) includes a generic example.
- From the brief: every link (with UTMs if the brand uses them) and the intent for image alt text. Figma supplies neither. A placeholder link is a blocker; `push` refuses it.

## Tools

Run `uv run scripts/f2k.py <command> --help` from this skill's folder. Dependencies install automatically.

| Command | Needs | Does |
|---|---|---|
| `fetch` | figma slug | `node_tree.json` and Figma's own render `figma-frame.png` |
| `outline` | offline | Readable layer outline: layout, padding, fills, text styles, `[HIDDEN, skip]` layers, `[MIXED STYLES]` |
| `fonts` | public font CSS | Fonts the design uses vs fonts registered in the brand's Klaviyo account |
| `convert` | offline | Tree + brand + overrides + assets → `template_body.json`, `decisions.json`, `todo.md`, `overrides.todo.json` |
| `assets` | figma slug | Exports exactly the images `convert` will place (pass the same `--overrides`, so skipped images are not exported), crops to the visible area, composites fill backgrounds → image files + `asset_plan.json` |
| `upload` | klaviyo slug | `--plan` uploads the planned files (hidden by default) → `assets.json`, re-uploading when `--slug` differs from the recorded one; `--image NAME=PATH --out DIR` uploads loose files → `uploads.json` |
| `validate` | offline | Schema check, TODO markers, placeholder links (exit 1 if any; `--allow-todo` tolerates TODOs in drafts) |
| `push` | klaviyo slug | Refuses TODOs and placeholder links, validates, then creates the template, or with `--template-id` replaces it |
| `check` | klaviyo slug | Renders through Klaviyo, checks text/alt/links/unsubscribe/size, screenshots desktop and real 375 px mobile (needs Chrome), builds `compare.png` |
| `export` | figma slug | Manual tool for text-free nodes only (`--tree` required, `--node ID=NAME`); refuses any node with live text. The converter uses `assets`. |

## Workflow

Work in a run folder, for example `runs/<date>-<email>/`, and keep the same paths through every step. Below, `$F` is the Figma file key, `$FRAME` the frame id, `$FIGMA` and `$KLAVIYO` the Loggie slugs.

1. **Fetch.** `fetch --figma-slug $FIGMA --file $F --frame $FRAME --out fetch/`
   - Read the printed JSON: frame name, size, and `file_version` (goes in the report).
   - Look at `figma-frame.png`, and run `outline --tree fetch/node_tree.json` once so you know the bands and hidden layers before judging the converter. Node ids in overrides come from the outline and `todo.md`.
2. **Fonts.** `fonts --tree fetch/node_tree.json --brand brand.json --company-id <klaviyo account id>`
   - Each row has `klaviyo`: `registered`, `family registered, this weight/style missing`, `NOT registered`, or `unchecked` (no `--company-id`).
   - Report every gap. A missing weight renders in the fallback font; a missing italic is faked by the browser. Never paper over it.
   - If a Figma family name differs from Klaviyo's (for example a "VF" variable font), fix `font_map` in the brand config.
3. **Convert (draft).** `convert --tree fetch/node_tree.json --brand brand.json --out draft/`
   - Read the printed summary (sections, block counts, TODO counts, decisions by confidence), then `draft/todo.md`: TODOs grouped by kind (`link`, `asset`, `alt`, `unmapped`) and the **Needs review** list (every low-confidence decision).
   - For each low-confidence item, accept it or fix it. [references/mapping-rules.md](references/mapping-rules.md) explains every rule name and what to do.
   - If the structure is wrong, prefer fixing the Figma frame (auto-layout, remove accidental overlaps) or overrides (`skip`, `roles`, `text`) over hand-editing JSON. See "When the converter's output is wrong".
4. **Assets.** `assets --tree fetch/node_tree.json --figma-slug $FIGMA --file $F --out assets/`
   - Run it where the Figma integration lives. If Loggie's figma access is on another machine, copy `scripts/` and the tree there, run it, and copy `assets/` back.
   - Read the printed summary. `errors` must be empty (the command exits non-zero otherwise; fix and re-run, `--only <node ids>` re-exports just those). Read every `warnings` entry: an unsupported blend mode or image filter in a composited background.
   - Once `overrides.json` has `skip` entries (step 5), re-run `assets --overrides overrides.json` so skipped images are not exported or uploaded.
   - Open the image files and look at them. Backgrounds and crops must match `figma-frame.png`.
   - Then `upload --slug $KLAVIYO --plan assets/asset_plan.json --prefix "<run tag> "`. This writes `assets/assets.json`, keyed by node id, and skips files whose sha256 and slug are unchanged (`--force` re-uploads; a different `--slug`, such as sandbox then production, re-uploads everything).
5. **Fill the worksheet.** Copy `draft/overrides.todo.json` to `overrides.json` and fill it (key semantics in [references/config.md](references/config.md)).
   - `links`: from the brief, with UTMs if the brand uses them.
   - `alt`: write it by LOOKING at the exported image and describing what it shows (brand name for a logo). Never use layer names.
   - Push refuses empty values and placeholder links such as `#`, `example.com`, `*.invalid`. Do not work around the gate.
6. **Convert (final).** `convert --tree fetch/node_tree.json --brand brand.json --overrides overrides.json --assets assets/assets.json --out final/`
   - The target is 0 TODOs in the summary. Anything left is listed in `final/todo.md`.
7. **Validate, then push.**
   - `validate --body final/template_body.json`: exit 0 means schema-clean, no TODOs, no placeholder links.
   - `push --slug $KLAVIYO --body final/template_body.json --expect-account <id>`. Use `--expect-account` when the key has `accounts:read`; otherwise run the `klaviyo` skill's canary check first.
   - Note the printed `template_id` and editor link. On every rerun add `--template-id <id>`: a PATCH replaces the whole definition.
8. **Check.** `check --slug $KLAVIYO --template-id <id> --tree fetch/node_tree.json --figma-render fetch/figma-frame.png --font-account <brand's Klaviyo account id> --out check/`
   - `--font-account` makes the screenshots use the brand's registered fonts in any sandbox. It affects only the screenshot copy.
   - `check.json` must show: `figma_text_missing_in_klaviyo` empty; `images_missing_alt` 0; `placeholder_links` empty; `unsubscribe_link` true; `leftover_tags` false; `gmail_clip_risk` false; `assets_unavailable` empty. If `assets_unavailable` lists URLs, the screenshots lack those images: wait and re-run `check`.
   - With no Chrome, `check` skips screenshots and the asset wait: `assets_unavailable` is `null` (not a pass) and `assets_note` says so. Install Chrome and re-run to review `compare.png`.
   - Open `check/compare.png` (Figma, Klaviyo desktop, Klaviyo 375 px mobile, side by side). Compare spacing, alignment, type roles, line breaks, crops, button sizes, mobile stacking.
   - Fix (Figma frame, overrides, or brand config), then repeat steps 6, 7 (with `--template-id`), and 8. Do up to 3 rounds, then report the remaining differences instead of looping.
9. **Hand off.** Run the `klaviyo` skill's pre-send QA if a campaign is involved, then report as below. Delete test templates and hide test images as the `klaviyo` skill describes.

## Report

```
Template: <name> (<id>) on <slug>. Editor: https://www.klaviyo.com/email-editor/<id>/edit
Source: Figma <file>/<frame> "<frame name>", version <file_version>
Check: text <n>/<n> present, alt ok, links from brief, unsubscribe ok, <html_kb> KB, assets_unavailable none
Fonts: <all registered | gaps and what renders instead>
Decisions: <n high / n medium / n low>. Low-confidence, one line each: <node id: rule: what you did>
Overrides used: <links, alt written, text, roles, skip>
Known differences: <card fills/borders not reproduced, button widths, line breaks, hard breaks on mobile, ...>
Post-processing: <none | what you changed by script and why>
For a person: inbox rendering and dark mode; approve before any send
```

## When the converter's output is wrong

In order of preference:
1. **Fix the Figma structure** and re-`fetch`: use auto-layout, remove accidental overlaps, hide leftover layers, keep text and its photo as separate layers. The design is the source of truth, and every later email benefits.
2. **Overrides:** `skip` a node, force a `roles` heading level, replace `text`. They are repeatable and recorded in `decisions.json`.
3. **Last resort: a small post-processing script** that loads `final/template_body.json`, adjusts blocks with the builders in `scripts/dnd.py`, writes a new body, and is validated and pushed like any other. State in the report exactly what it changed.
4. **Never image slices.** If live text can't reproduce the design, flag it with options.

## Gotchas

- **Hidden layers:** Figma frames often hide leftovers ("Lorem Ipsum", a second logo). The converter skips hidden and zero-opacity subtrees (`skipped-hidden`).
- **Export links expire.** Figma's export URLs are short-lived; `assets` downloads immediately. Never store or reuse one. If a run fails midway, re-run `assets` (`--only` for a subset).
- **Fresh uploads lag.** Newly uploaded images can take a moment to serve; `check` polls for up to about 60 s in total and reports stragglers in `assets_unavailable`.
- **Hard line breaks carry to mobile.** A Figma `\n` tuned for desktop becomes a `<br>` and wraps awkwardly on a phone. Remove the break in Figma (let the text box wrap) if mobile looks ragged.
- **Fonts fall back.** Gmail and other clients without web-font support show the stack's fallback (for example Georgia), so line breaks and widths differ from Figma. List this under known differences.
- **Logos** are exported as PNG with `max_width` set, or they would stretch on mobile. Photos are JPEG flattened on the band color.
- **Rate limits:** drag-and-drop template writes are limited to about 3/s and 60/m. Image uploads are capped at 500/day through `/image-upload`.
- **Brief-only inputs:** Figma has no links, alt text, or mobile design. Alt text and mobile choices are your decisions; list them in the report.
- **Node-render failure:** if Figma's batched node-render call fails, `assets` exits without writing `asset_plan.json`. Re-run it.
- **Subject line and preview text** go on the campaign message (see the `klaviyo` skill), not the template.

## Reference

- [references/mapping-rules.md](references/mapping-rules.md): what the converter does and why, every decision rule name, known limitations.
- [references/config.md](references/config.md): brand config keys, override keys, `assets.json` and `asset_plan.json`.
