#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.18", "referencing>=0.30", "pillow>=10.1"]
# ///
"""Figma → Klaviyo toolkit. Reads Figma through a Loggie `figma` integration (REST, read-only)
and writes to Klaviyo through a Loggie `klaviyo` integration.

  fetch     frame node tree + Figma's own render of the frame
  outline   readable layer outline (layout, fills, text styles, hidden layers)
  export    PNG/JPEG exports of image nodes at 2x
  assets    IR-driven image export: crop overflow, composite fill stacks → asset_plan.json
  convert   offline Figma tree → editable template draft + decisions + TODO worksheet
  fonts     fonts the design uses vs fonts registered in the brand's Klaviyo account
  upload    upload images to Klaviyo (hidden by default) → uploads.json, or --plan → assets.json
  validate  check a template body against Klaviyo's schema and reject TODOs/placeholders
  push      create (POST) or update (PATCH) the template
  check     render, screenshot desktop + mobile, text/link/alt checks, comparison board

Run with `uv run scripts/f2k.py <command> --help`.
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from emit import emit, find_todos
from ir import IR, Box, Button, Image, Text, Unknown, build_ir, is_hidden, topmost_color

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schemas" / "klaviyo-templates-2026-07-15.json"
EDITOR_URL = "https://www.klaviyo.com/email-editor/{id}/edit"
BRAND_KEYS = {"font_map", "fallback_stacks", "mobile", "link_color", "page_background", "footer", "gutter"}
OVERRIDE_KEYS = {"links", "alt", "text", "roles", "skip"}
FONT_CSS_URL = "https://static-forms.klaviyo.com/fonts/api/v1/{id}/custom_fonts.css"


def font_css_url(company_id: str) -> str:
    return FONT_CSS_URL.format(id=urllib.parse.quote(company_id, safe=""))


def read_json_object(filename: str, label: str, allowed_keys: set[str] | None = None) -> dict:
    try:
        contents = Path(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        sys.exit(f"cannot read {label} {filename}: {exc}")
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid JSON in {label} {filename}: {exc}")
    if not isinstance(value, dict):
        sys.exit(f"{label} {filename} must contain a JSON object")
    if allowed_keys is not None and (unknown := value.keys() - allowed_keys):
        sys.exit(f"unknown {label} keys: {', '.join(sorted(unknown))} (in {filename})")
    return value


SAFE_FILENAME = re.compile(r"[A-Za-z0-9._-]+")
ROLE_VALUES = {"text", "h1", "h2", "h3", "h4"}


def check_override_shapes(overrides: dict, filename: str | None) -> None:
    """Exit with a clear message unless every override has the shape emit expects."""
    where = f"overrides {filename}"
    for key in ("links", "alt", "text", "roles"):
        value = overrides.get(key, {})
        if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            sys.exit(f"{where}: {key} must be an object mapping node ids to strings")
    bad = sorted({v for v in overrides.get("roles", {}).values() if v not in ROLE_VALUES})
    if bad:
        sys.exit(f"{where}: roles values must be one of {', '.join(sorted(ROLE_VALUES))}; got {', '.join(bad)}")
    skip = overrides.get("skip", [])
    if not isinstance(skip, list) or not all(isinstance(i, str) for i in skip):
        sys.exit(f"{where}: skip must be a list of node id strings")


def read_nodes(filename: str) -> dict:
    """The `nodes` map of a Figma nodes response (what `fetch` writes)."""
    nodes = read_json_object(filename, "tree").get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        sys.exit(f"tree {filename} has no 'nodes' map (expected the output of `fetch`)")
    return nodes


# ---------------------------------------------------------------------------
# Loggie
# ---------------------------------------------------------------------------

class LoggieError(RuntimeError):
    pass


def loggie(slug: str, method: str, path: str, *, query: str | None = None, body: dict | None = None,
           form: list[str] | None = None) -> dict:
    """Call Loggie with stdin closed (Loggie reads a non-terminal stdin as the request body)."""
    cmd = ["loggie", "call", slug, method, path]
    if query:
        cmd += ["--query", query]
    if body is not None:
        cmd += ["--body", json.dumps(body, ensure_ascii=False)]
    for field in form or []:
        cmd += ["-F", field]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        sys.exit("loggie not found: install the Loggie CLI and put it on PATH")
    out = proc.stdout.strip()
    data = json.loads(out) if out.startswith(("{", "[")) else {}
    if proc.returncode != 0:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        details = err.get("details", {})
        upstream = (details.get("upstream_body") or {}).get("errors") or []
        detail = upstream[0].get("detail") if upstream else err.get("message") or proc.stderr.strip()
        raise LoggieError(f"{method} {path} failed ({details.get('upstream_status') or err.get('code')}): {detail}")
    return data


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# ---------------------------------------------------------------------------
# fetch / outline / export
# ---------------------------------------------------------------------------

def cmd_fetch(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tree = loggie(a.figma_slug, "GET", f"/v1/files/{a.file}/nodes", query=f"ids={a.frame}")
    if a.frame not in tree.get("nodes", {}):
        sys.exit(f"frame {a.frame} not found in file {a.file}")
    (out / "node_tree.json").write_text(json.dumps(tree, ensure_ascii=False))
    render = loggie(a.figma_slug, "GET", f"/v1/images/{a.file}", query=f"ids={a.frame}&format=png&scale=1")
    download(render["images"][a.frame], out / "figma-frame.png")
    doc = tree["nodes"][a.frame]["document"]
    b = doc["absoluteBoundingBox"]
    print(json.dumps({"frame": doc["name"], "size": [b["width"], b["height"]], "file_version": tree.get("version"),
                      "wrote": ["node_tree.json", "figma-frame.png"]}))


def cmd_outline(a):
    tree = read_nodes(a.tree)
    frame_id = a.frame or next(iter(tree))
    if frame_id not in tree:
        sys.exit(f"frame {frame_id} not found in tree {a.tree}")
    root = tree[frame_id]["document"]
    origin = root["absoluteBoundingBox"]

    def walk(n, depth=0):
        b = n.get("absoluteBoundingBox") or {}
        x, y = round(b.get("x", 0) - origin["x"]), round(b.get("y", 0) - origin["y"])
        line = f"{'  ' * depth}{n['id']} {n['type']} '{n['name']}' @({x},{y}) {round(b.get('width', 0))}x{round(b.get('height', 0))}"
        if is_hidden(n):
            print(line + " [HIDDEN, skip]")
            return
        if n.get("layoutMode"):
            pad = [int(n.get(k, 0)) for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")]
            line += f" {n['layoutMode'][0]}-autolayout pad={pad} gap={n.get('itemSpacing', 0)}"
        elif n.get("children") and depth > 0:
            line += " (absolute children)"
        fill = topmost_color(n)
        if fill:
            line += f" fill={fill}"
        if any(p.get("type") == "IMAGE" for p in n.get("fills") or []):
            line += " IMAGE-FILL"
        if n.get("strokes"):
            line += f" stroke={topmost_color(n, 'strokes')}/{n.get('strokeWeight')}"
        if n["type"] == "TEXT":
            s = n["style"]
            line += (f"\n{'  ' * (depth + 1)}\"{n.get('characters', '')[:100]}\"\n{'  ' * (depth + 1)}"
                     f"{s.get('fontFamily')} {s.get('fontPostScriptName')} {s.get('fontSize'):g}/{s.get('lineHeightPx', 0):g} "
                     f"w{s.get('fontWeight')}{' italic' if s.get('italic') else ''} ls={s.get('letterSpacing', 0):g} "
                     f"case={s.get('textCase', '-')} align={s.get('textAlignHorizontal')} color={topmost_color(n)}")
            if s.get("hyperlink"):
                line += f" link={s['hyperlink']}"
            if any(n.get("characterStyleOverrides") or []):
                line += " [MIXED STYLES]"
        print(line)
        for c in n.get("children") or []:
            walk(c, depth + 1)
    walk(root)


def split_pairs(items: list[str], option: str, shape: str) -> list[tuple[str, str]]:
    """Split repeated `LEFT=RIGHT` option values; exit with a clear message on a malformed one."""
    pairs = []
    for item in items:
        left, sep, right = item.partition("=")
        if not sep or not left or not right:
            sys.exit(f"{option} {item!r} must look like {shape}")
        pairs.append((left, right))
    return pairs


def safe_name(name: str) -> str:
    """Human-readable file stem: letters, digits, '-', '_', '.'; never empty, never a dot-only name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")


def text_ids_under(node: dict) -> list[str]:
    """Ids of TEXT nodes in the node's visible subtree (hidden layers are not exported either)."""
    if is_hidden(node):
        return []
    found = [node["id"]] if node.get("type") == "TEXT" else []
    for child in node.get("children") or []:
        found += text_ids_under(child)
    return found


def cmd_export(a):
    """Manual tool for text-free nodes. Live text must never become an image, so any node whose
    visible subtree holds a TEXT node is refused; the converter uses `assets` instead."""
    tree = read_json_object(a.tree, "tree")
    roots = [entry["document"] for entry in (tree.get("nodes") or {}).values()
             if isinstance(entry, dict) and "document" in entry] or [tree.get("document", tree)]
    index: dict[str, dict] = {}
    stack = list(roots)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            index.setdefault(str(node.get("id")), node)
            stack.extend(node.get("children") or [])
    pairs = split_pairs(a.node, "--node", "ID=NAME")
    names: dict[str, str] = {}
    for node_id, name in pairs:
        if node_id not in index:
            sys.exit(f"export: node {node_id} not found in tree {a.tree}")
        texts = text_ids_under(index[node_id])
        if texts:
            sys.exit(f"export refused: node {node_id} contains live text ({', '.join(texts[:5])}); "
                     "export text-free nodes only, and keep text live in the template")
        stem = safe_name(name)
        if not stem:
            sys.exit(f"export: NAME {name!r} for node {node_id} has no usable characters")
        if stem in names.values():
            sys.exit(f"export: duplicate output name {stem!r}")
        names[node_id] = stem
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    for stem in names.values():
        if not (root / f"{stem}.png").resolve().is_relative_to(root):
            sys.exit(f"export: name {stem!r} escapes {a.out}")
    res = loggie(a.figma_slug, "GET", f"/v1/images/{a.file}",
                 query=f"ids={','.join(names)}&format=png&scale={a.scale}")
    if res.get("err"):
        sys.exit(f"figma export error: {res['err']}")
    jpeg = set(a.jpeg.split(",")) if a.jpeg else set()
    written = {}
    for node_id, name in names.items():
        url = (res.get("images") or {}).get(node_id)
        if not url:
            print(f"no render for {node_id}", file=sys.stderr)
            continue
        png = out / f"{name}.png"
        download(url, png)
        if name in jpeg:
            from PIL import Image
            jpg = out / f"{name}.jpg"
            Image.open(png).convert("RGB").save(jpg, "JPEG", quality=82, optimize=True)
            png.unlink()
            png = jpg
        written[name] = png.name
    print(json.dumps(written))


# ---------------------------------------------------------------------------
# assets: IR -> exported, cropped, composited image files
# ---------------------------------------------------------------------------

def _plan_box(value: dict | None) -> Box | None:
    return None if value is None else Box(value["x"], value["y"], value["w"], value["h"])


def _short_error(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:200]


def cmd_assets(a):
    from assets import asset_plan, composite_fill, crop_to_visible, save_asset
    from PIL import Image as PILImage

    tree = read_json_object(a.tree, "tree")
    overrides = read_json_object(a.overrides, "overrides", OVERRIDE_KEYS) if a.overrides else {}
    check_override_shapes(overrides, a.overrides)
    if a.frame is not None and "nodes" in tree and a.frame not in tree["nodes"]:
        sys.exit(f"frame {a.frame} not found in tree {a.tree}")
    try:
        ir = build_ir(tree, a.frame)
    except StopIteration:
        sys.exit(f"frame {a.frame} not found in tree {a.tree}")
    except (KeyError, TypeError, ValueError) as exc:
        sys.exit(f"invalid Figma tree {a.tree}: {exc}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    plan = asset_plan(ir, a.scale, overrides)
    plan_path = out / "asset_plan.json"
    previous = {}
    if plan_path.exists():
        try:
            previous = {e["node_id"]: e.get("warnings", []) for e in json.loads(plan_path.read_text())}
        except (ValueError, KeyError, TypeError):
            previous = {}
    for entry in plan:
        entry["warnings"] = list(previous.get(entry["node_id"], []))
    only = {i for i in a.only.split(",") if i} if a.only else None
    unknown = sorted(only - {e["node_id"] for e in plan}) if only else []
    if unknown:
        sys.exit(f"--only ids not in the asset plan: {', '.join(unknown)}")
    selected = [e for e in plan if only is None or e["node_id"] in only]
    written: dict[str, dict] = {}
    errors: dict[str, str] = {}

    node_entries = [e for e in selected if e["export"] == "node"]
    if node_entries:
        res = loggie(a.figma_slug, "GET", f"/v1/images/{a.file}",
                     query=f"ids={','.join(e['node_id'] for e in node_entries)}&format=png"
                           f"&scale={a.scale}&use_absolute_bounds=true")
        if res.get("err"):
            sys.exit(f"figma export error: {res['err']}")
        urls = res.get("images") or {}
        with tempfile.TemporaryDirectory() as tmp:
            for entry in node_entries:
                url = urls.get(entry["node_id"])
                if not url:
                    errors[entry["node_id"]] = "no render returned by Figma"
                    continue
                try:
                    raw = Path(tmp) / f"{entry['filename']}.png"
                    download(url, raw)
                    with PILImage.open(raw) as opened:
                        img = crop_to_visible(opened.convert("RGBA"), _plan_box(entry["box"]),
                                              _plan_box(entry["clipped_box"]), a.scale)
                    entry["warnings"] = []
                    save_asset(img, out / entry["filename"], entry["format"], entry["matte"])
                    written[entry["node_id"]] = {"file": entry["filename"], "size": list(img.size)}
                except (LoggieError, OSError, ValueError, KeyError, TypeError) as exc:  # one bad image must not abort the run
                    errors[entry["node_id"]] = _short_error(exc)

    fill_entries = [e for e in selected if e["export"] == "fill"]
    if fill_entries:
        refs: dict = {}
        try:
            res = loggie(a.figma_slug, "GET", f"/v1/files/{a.file}/images")
        except LoggieError as exc:
            for entry in fill_entries:
                errors[entry["node_id"]] = f"image fills lookup failed: {_short_error(exc)}"
            fill_entries = []
        else:
            if res.get("err"):
                sys.exit(f"figma image fills error: {res['err']}")
            refs = (res.get("meta") or {}).get("images") or res.get("images") or {}
        with tempfile.TemporaryDirectory() as tmp:
            cache: dict[str, PILImage.Image] = {}
            for entry in fill_entries:
                wanted = [p["imageRef"] for p in entry["paints"] if p.get("type") == "IMAGE" and p.get("imageRef")]
                missing = [r for r in wanted if r not in refs]
                if missing or not wanted:
                    errors[entry["node_id"]] = ("no image URL for imageRef " + ", ".join(missing)
                                                if missing else "fill export has no IMAGE paint")
                    continue
                try:
                    for ref in wanted:
                        if ref not in cache:
                            raw = Path(tmp) / f"{len(cache)}.img"
                            download(refs[ref], raw)
                            with PILImage.open(raw) as opened:
                                cache[ref] = opened.convert("RGBA")
                    img, warnings = composite_fill(cache, entry["paints"], _plan_box(entry["box"]),
                                                   a.scale, _plan_box(entry["clipped_box"]),
                                                   entry["matte"])
                    entry["warnings"] = warnings
                    save_asset(img, out / entry["filename"], entry["format"], entry["matte"])
                    written[entry["node_id"]] = {"file": entry["filename"], "size": list(img.size)}
                except (LoggieError, OSError, ValueError, KeyError, TypeError) as exc:  # one bad image must not abort the run
                    errors[entry["node_id"]] = _short_error(exc)

    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"plan": str(plan_path), "written": written,
               "warnings": {e["node_id"]: e["warnings"] for e in selected if e["warnings"]},
               "errors": errors}
    print(json.dumps(summary, ensure_ascii=False))
    if errors:
        sys.exit("asset export failed for: " + ", ".join(errors))


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------

def image_context(image: Image) -> str:
    box = image.clipped_box or image.box
    return f'image "{image.name_hint}" (visible {round(box.w)}×{round(box.h)} px)'


def todo_markdown(ir: IR, todos: list[str], decisions: list[dict], overrides: dict) -> str:
    contexts = {}
    for band in ir.bands:
        contexts[band.node_id] = f"band {band.name}"
        for row in band.rows:
            for cell in row.cells:
                for element in cell.elements:
                    if isinstance(element, Button):
                        label = overrides.get("text", {}).get(element.label.node_id, element.label.characters)
                        context = f'button "{label}"'
                    elif isinstance(element, Image):
                        context = image_context(element)
                    elif isinstance(element, Text):
                        context = overrides.get("text", {}).get(element.node_id, element.characters)
                    elif isinstance(element, Unknown):
                        context = f"{element.node_type}: {element.reason}"
                    else:
                        context = element.kind
                    contexts[element.node_id] = context
        if band.background_image:
            contexts[band.background_image.node_id] = (
                f"section background of band {band.name}; {image_context(band.background_image)}")
    grouped: dict[str, list[str]] = {kind: [] for kind in ("link", "asset", "alt", "unmapped")}
    for marker in todos:
        _, kind, node_id = marker.split(":", 2)
        context = " ".join(contexts.get(node_id, "Review supplied input").split())
        grouped.setdefault(kind, []).append(f"- `{marker}` — {context}")
    lines = ["# TODOs", ""]
    for kind, entries in grouped.items():
        lines.extend([f"## {kind}", "", *(entries or ["None."]), ""])
    lines.extend(["## Needs review", ""])
    lines.extend(f"- `{decision['node_id']}` — {decision['rule']}: {decision['detail']}"
                 for decision in decisions if decision["confidence"] == "low")
    if not any(decision["confidence"] == "low" for decision in decisions):
        lines.append("None.")
    return "\n".join(lines) + "\n"


def cmd_convert(a):
    tree = read_json_object(a.tree, "tree")
    brand = read_json_object(a.brand, "brand", BRAND_KEYS) if a.brand else {}
    overrides = read_json_object(a.overrides, "overrides", OVERRIDE_KEYS) if a.overrides else {}
    check_override_shapes(overrides, a.overrides)
    assets = read_json_object(a.assets, "assets") if a.assets else {}
    if "gutter" in brand:
        gutter = brand["gutter"]
        if isinstance(gutter, bool) or not isinstance(gutter, (int, float)) or gutter < 0:
            sys.exit(f"brand {a.brand}: gutter must be a non-negative number, got {gutter!r}")
    if a.frame is not None and "nodes" in tree and a.frame not in tree["nodes"]:
        sys.exit(f"frame {a.frame} not found in tree {a.tree}")
    try:
        ir = build_ir(tree, a.frame)
    except StopIteration:
        sys.exit(f"frame {a.frame} not found in tree {a.tree}")
    except (KeyError, TypeError, ValueError) as exc:
        sys.exit(f"invalid Figma tree {a.tree}: {exc}")
    conversion = emit(ir, brand=brand, overrides=overrides, assets=assets, name=a.name)
    todos = conversion.todos
    worksheet = dict(overrides)
    for kind in ("links", "alt"):
        worksheet[kind] = dict(overrides.get(kind, {}))
    todo_counts = dict.fromkeys(("link", "asset", "alt", "unmapped"), 0)
    for marker in todos:
        _, kind, node_id = marker.split(":", 2)
        todo_counts[kind] = todo_counts.get(kind, 0) + 1
        if kind in ("link", "alt"):
            worksheet["links" if kind == "link" else "alt"].setdefault(node_id, "")
    files = {"template_body.json": conversion.body, "decisions.json": conversion.decisions,
             "overrides.todo.json": worksheet}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for filename, data in files.items():
        (out / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "todo.md").write_text(todo_markdown(ir, todos, conversion.decisions, overrides), encoding="utf-8")
    sections = conversion.body["data"]["attributes"]["definition"]["body"]["sections"]
    summary = {"sections": len(sections),
               "blocks": dict(Counter(block["type"] for section in sections for row in section["rows"]
                                      for column in row["columns"] for block in column["blocks"])),
               "todos": todo_counts,
               "decisions": {confidence: sum(d["confidence"] == confidence for d in conversion.decisions)
                             for confidence in ("high", "medium", "low")},
               "outputs": {filename: str(out / filename) for filename in (*files, "todo.md")}}
    if todos:
        summary["push"] = "blocked until TODOs are resolved"
    print(json.dumps(summary, ensure_ascii=False))


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------

def fonts_needed(tree_path: str) -> set[tuple[str, int, bool]]:
    tree = read_nodes(tree_path)
    need = set()

    def walk(n):
        if is_hidden(n):
            return
        if n["type"] == "TEXT":
            styles = [n["style"], *(n.get("styleOverrideTable") or {}).values()]
            for s in styles:
                if s.get("fontFamily"):
                    need.add((s["fontFamily"], int(s.get("fontWeight", 400)), bool(s.get("italic"))))
        for c in n.get("children") or []:
            walk(c)
    for v in tree.values():
        walk(v["document"])
    return need


def fonts_registered(company_id: str) -> set[tuple[str, int, bool]]:
    css = urllib.request.urlopen(font_css_url(company_id), timeout=60).read().decode("utf-8", "replace")
    have = set()
    for face in re.findall(r"@font-face\s*{([^}]*)}", css):
        fam = re.search(r"font-family:\s*['\"]?([^;'\"]+?)['\"]?\s*;", face)
        wt = re.search(r"font-weight:\s*(\d+)", face)
        st = re.search(r"font-style:\s*(\w+)", face)
        if fam:
            have.add((fam.group(1).strip(), int(wt.group(1)) if wt else 400, bool(st and st.group(1) == "italic")))
    return have


def cmd_fonts(a):
    brand = read_json_object(a.brand, "brand", BRAND_KEYS) if a.brand else {}
    need = fonts_needed(a.tree)
    have = fonts_registered(a.company_id) if a.company_id else set()
    families = {f for f, _, _ in have}
    rows = []
    for fam, wt, it in sorted(need):
        klaviyo_family = brand.get("font_map", {}).get(fam, {}).get("klaviyo_family", fam)
        if not a.company_id:
            status = "unchecked"
        elif (klaviyo_family, wt, it) in have:
            status = "registered"
        elif klaviyo_family in families:
            status = "family registered, this weight/style missing"
        else:
            status = "NOT registered"
        row = {"family": fam, "weight": wt, "italic": it, "klaviyo": status}
        if a.brand:
            row["klaviyo_family"] = klaviyo_family
        rows.append(row)
    print(json.dumps(rows, indent=1))
    if a.company_id:
        print(f"registered families: {sorted(families)}", file=sys.stderr)


# ---------------------------------------------------------------------------
# upload / validate / push
# ---------------------------------------------------------------------------

def cmd_upload_plan(a):
    plan_path = Path(a.plan)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read plan {a.plan}: {exc}")
    folder = plan_path.parent
    if not isinstance(plan, list) or not all(isinstance(e, dict) for e in plan):
        sys.exit(f"plan {a.plan} must be a JSON array of asset entries")
    for e in plan:
        filename = e.get("filename")
        if not isinstance(filename, str) or not SAFE_FILENAME.fullmatch(filename) or filename in {".", ".."}:
            sys.exit(f"plan {a.plan}: unsafe filename {filename!r}; expected letters, digits, . _ - only")
        if not (folder / filename).resolve().is_relative_to(folder.resolve()):
            sys.exit(f"plan {a.plan}: filename {filename!r} escapes the plan folder")
    missing = [e["filename"] for e in plan if not (folder / e["filename"]).is_file()]
    if missing:
        sys.exit("planned files missing (run `assets` first): " + ", ".join(missing))
    record = folder / "assets.json"
    assets: dict = {}
    if record.exists():
        try:
            assets = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(assets, dict):
                raise TypeError("expected a JSON object")
        except (OSError, ValueError, TypeError) as exc:
            sys.exit(f"cannot read {record}: {exc}; fix or delete it")
    for entry in plan:
        node_id = entry["node_id"]
        path = folder / entry["filename"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        known = assets.get(node_id)
        if (isinstance(known, dict) and known.get("sha256") == digest and known.get("slug") == a.slug
                and not a.force):
            continue  # a different (or unrecorded) Klaviyo slug means another account: upload again
        res = loggie(a.slug, "POST", "/image-upload",
                     form=[f"file=@{path}", f"name={a.prefix}{entry['filename']}",
                           f"hidden={'false' if a.visible else 'true'}"])
        try:
            assets[node_id] = {"id": res["data"]["id"], "url": res["data"]["attributes"]["image_url"],
                               "sha256": digest, "slug": a.slug}
        except (KeyError, TypeError):
            sys.exit(f"upload response for {node_id} is missing data.id / data.attributes.image_url")
        record.write_text(json.dumps(assets, indent=1))
        print(json.dumps({node_id: assets[node_id]}))
    record.write_text(json.dumps(assets, indent=1))


def cmd_upload(a):
    if a.plan:
        if a.image:
            sys.exit("upload: use either --plan or --image, not both")
        return cmd_upload_plan(a)
    if not a.image or not a.out:
        sys.exit("upload: --image and --out are required without --plan")
    out = Path(a.out)
    record = out / "uploads.json"
    uploads = read_json_object(str(record), "uploads record") if record.exists() else {}
    for name, path in split_pairs(a.image, "--image", "NAME=PATH"):
        res = loggie(a.slug, "POST", "/image-upload",
                     form=[f"file=@{path}", f"name={a.prefix}{name}", f"hidden={'false' if a.visible else 'true'}"])
        uploads[name] = {"id": res["data"]["id"], "url": res["data"]["attributes"]["image_url"]}
        print(json.dumps({name: uploads[name]}))
    record.write_text(json.dumps(uploads, indent=1))


def placeholder_hrefs(body: object) -> list[str]:
    """Find placeholder href properties and HTML links, including in rendered HTML."""
    from html.parser import HTMLParser

    hrefs: list[str] = []

    class HrefParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.hrefs.extend(value or "" for key, value in attrs if key == "href")

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "href" and isinstance(child, str):
                    hrefs.append(child)
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            parser = HrefParser()
            try:
                parser.feed(value)
                parser.close()
            except Exception:  # noqa: BLE001 - malformed HTML must not escape the collector.
                return
            hrefs.extend(parser.hrefs)

    walk(body)
    placeholders: set[str] = set()
    for raw in hrefs:
        href = htmllib.unescape(raw).strip()
        if "{%" in href or "{{" in href or href.lower().startswith(("mailto:", "tel:")):
            continue
        try:
            parsed = urllib.parse.urlsplit(href)
            host = parsed.hostname or ""
        except ValueError:
            placeholders.add(href)
            continue
        host = host.lower().rstrip(".")
        if (href in ("", "#") or href.startswith("TODO") or href.lower().startswith("javascript:")
                or (parsed.scheme in ("http", "https") and not host)
                or host == "localhost" or host.endswith((".example", ".invalid", ".test", ".localhost"))
                or any(host == domain or host.endswith("." + domain)
                       for domain in ("example.com", "example.org", "example.net"))):
            placeholders.add(href)
    return sorted(placeholders)


def validate_body(body: dict) -> list[str]:
    import referencing.jsonschema as rj
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    spec = json.loads(SCHEMA.read_text())

    def nullable(node):
        if isinstance(node, dict):
            if node.get("nullable") and isinstance(node.get("type"), str):
                node["type"] = [node["type"], "null"]
            for v in node.values():
                nullable(v)
        elif isinstance(node, list):
            for v in node:
                nullable(v)
    nullable(spec)
    ref = spec["paths"]["/api/templates"]["post"]["requestBody"]["content"]["application/vnd.api+json"]["schema"]["$ref"]
    registry = Registry().with_resource("spec", Resource.from_contents(spec, default_specification=rj.DRAFT202012))
    validator = Draft202012Validator({"$ref": "spec" + ref}, registry=registry)
    errors = []
    for e in validator.iter_errors(body):
        # oneOf failures hide the real cause; report the deepest, most specific error.
        best = e
        while best.context:
            best = min(best.context, key=lambda c: (-len(c.absolute_path), len(c.message)))
        errors.append(f"{'/'.join(map(str, e.absolute_path))}: {best.message[:200]} "
                      f"(at {'/'.join(map(str, best.absolute_path))})")
    return errors


def cmd_validate(a):
    body = read_json_object(a.body, "body")
    errors = validate_body(body)
    todos = find_todos(body)
    placeholders = placeholder_hrefs(body)
    print(json.dumps({"schema_errors": len(errors), "errors": errors[:20],
                      "todo_markers": {"count": len(todos), "markers": todos},
                      "placeholder_links": placeholders}, indent=1))
    sys.exit(1 if errors or ((todos or placeholders) and not a.allow_todo) else 0)


def cmd_push(a):
    body = read_json_object(a.body, "body")
    todos = find_todos(body)
    placeholders = placeholder_hrefs(body)
    if todos or placeholders:
        print(json.dumps({"todo_markers": {"count": len(todos), "markers": todos},
                          "placeholder_links": placeholders}, indent=1))
        sys.exit("resolve TODO markers and placeholder links before pushing")
    errors = validate_body(body)
    if errors:
        print(json.dumps({"schema_errors": errors[:10]}, indent=1))
        sys.exit("fix schema errors before pushing")
    if a.expect_account:
        acct = loggie(a.slug, "GET", "/accounts")["data"][0]["id"]
        if acct != a.expect_account:
            sys.exit(f"account mismatch: expected {a.expect_account}, got {acct}")
    if a.template_id:
        patch = {"data": {"type": "template", "id": a.template_id,
                          "attributes": {"definition": body["data"]["attributes"]["definition"]}}}
        loggie(a.slug, "PATCH", f"/templates/{a.template_id}", body=patch)
        tid = a.template_id
        action = "patched (PATCH replaces the whole definition; 'updated' may not change)"
    else:
        res = loggie(a.slug, "POST", "/templates", body=body)
        tid = res["data"]["id"]
        action = "created"
    print(json.dumps({"template_id": tid, "action": action, "editor": EDITOR_URL.format(id=tid)}))


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def chrome() -> str | None:
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")):
        if c and Path(c).exists():
            return c
    return None


VIRTUAL_TIME_BUDGET_MS = 10000


def screenshot_command(chrome_bin: str, target: Path, png: Path, width: int, height: int) -> list[str]:
    # The virtual-time budget lets slow network images finish before the shot is taken.
    return [chrome_bin, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
            f"--window-size={width},{height}", f"--screenshot={png}", target.as_uri()]


def screenshot(chrome_bin: str, target: Path, png: Path, width: int, height: int) -> None:
    subprocess.run(screenshot_command(chrome_bin, target, png, width, height),
                   capture_output=True, timeout=180, check=False)


_TAG = re.compile(r"<([a-zA-Z][\w:-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_ATTR = re.compile(r"([^\s=/\"'<>]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
_CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)\s]*))", re.IGNORECASE)


def asset_urls(doc: str) -> list[str]:
    """Every <img src>, background attribute, and style-attribute url(...) in document order, de-duplicated."""
    found: list[str] = []
    for tag in _TAG.finditer(doc):
        name = tag.group(1).lower()
        for attr in _ATTR.finditer(tag.group(2)):
            key = attr.group(1).lower()
            value = attr.group(2) or attr.group(3) or attr.group(4) or ""
            if (key == "src" and name == "img") or key == "background":
                found.append(value)
            elif key == "style":
                found += [m[0] or m[1] or m[2] for m in _CSS_URL.findall(htmllib.unescape(value))]
    urls = []
    for raw in found:
        u = htmllib.unescape(raw).strip()
        urls.append("https:" + u if u.startswith("//") else u)
    return list(dict.fromkeys(u for u in urls if u.lower().startswith(("http://", "https://"))))


def url_status(url: str, timeout: float = 15) -> int:
    """HTTP status of a tiny ranged GET; 0 when the request itself fails."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "f2k-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def wait_for_assets(urls: list[str], *, timeout: float = 60, interval: float = 2, workers: int = 8,
                    status=url_status, sleep=time.sleep, clock=time.monotonic) -> list[str]:
    """Poll each URL until it answers HTTP 200 (or 206 for a range); return those that never did.

    The deadline is checked before every request and bounds its timeout, so the wait cannot
    overrun `timeout` by more than one request's worth. Pending URLs are polled concurrently.
    """
    start = clock()

    def probe(url: str) -> bool:
        remaining = timeout - (clock() - start)
        if remaining <= 0:
            return False
        return status(url, min(15, remaining)) in (200, 206)

    pending = list(urls)
    while pending:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            results = list(pool.map(probe, pending))
        pending = [u for u, ok in zip(pending, results) if not ok]
        if not pending or clock() - start + interval > timeout:
            break
        sleep(interval)
    return pending


def with_font_link(doc: str, company_id: str) -> str:
    link = f'<link rel="stylesheet" href="{font_css_url(company_id)}">'
    if re.search(r"</head\s*>", doc, re.IGNORECASE):
        return re.sub(r"</head\s*>", lambda m: link + m.group(0), doc, count=1, flags=re.IGNORECASE)
    if re.search(r"<head\b[^>]*>", doc, re.IGNORECASE):
        return re.sub(r"<head\b[^>]*>", lambda m: m.group(0) + link, doc, count=1, flags=re.IGNORECASE)
    return f"<head>{link}</head>{doc}"


def visible_text(html_doc: str) -> str:
    body = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html_doc)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", htmllib.unescape(body).replace("\xa0", " ")).strip()


def cmd_check(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        context = json.loads(a.context) if a.context else {}
    except ValueError as exc:
        sys.exit(f"invalid JSON in --context: {exc}")
    if not isinstance(context, dict):
        sys.exit("--context must be a JSON object")
    figma_nodes = read_nodes(a.tree) if a.tree else None  # fail before any network call
    res = loggie(a.slug, "POST", "/template-render",
                 body={"data": {"type": "template", "id": a.template_id, "attributes": {"context": context}}})
    doc = res["data"]["attributes"]["html"]
    (out / "rendered.html").write_text(doc)

    report = {"template_id": a.template_id, "editor": EDITOR_URL.format(id=a.template_id),
              "html_kb": round(len(doc.encode()) / 1024, 1)}
    report["gmail_clip_risk"] = report["html_kb"] > 100
    report["leftover_tags"] = bool(re.search(r"\{\{|\{%", visible_text(doc)))
    report["unsubscribe_link"] = bool(re.search(r"unsubscribe", doc, re.IGNORECASE))
    imgs = re.findall(r"<img\b[^>]*>", doc, re.IGNORECASE)
    report["images"] = len(imgs)
    report["images_missing_alt"] = sum(1 for t in imgs if not re.search(r'\balt="[^"]+"', t))
    report["placeholder_links"] = placeholder_hrefs(doc)

    if a.tree:
        tree = figma_nodes
        texts = []

        def walk(n):
            if is_hidden(n):
                return
            if n["type"] == "TEXT":
                t = n.get("characters", "")
                case = n["style"].get("textCase")
                texts.append(t.upper() if case == "UPPER" else t)
            for c in n.get("children") or []:
                walk(c)
        for v in tree.values():
            walk(v["document"])
        rendered = visible_text(doc).casefold()
        norm = lambda s: re.sub(r"\s+", " ", s.replace(" ", " ")).strip().casefold()
        missing = [t for t in texts if norm(t) and norm(t) not in rendered]
        report["figma_text_nodes"] = len(texts)
        report["figma_text_missing_in_klaviyo"] = missing

    chrome_bin = chrome()
    if chrome_bin:
        report["assets_unavailable"] = wait_for_assets(asset_urls(doc))
    else:
        report["assets_unavailable"] = None
        report["assets_note"] = "Chrome not found; asset availability wait skipped (no screenshots will run)."
    if chrome_bin:
        shot_page = out / "rendered.html"
        if a.font_account:
            shot_page = out / "rendered-screenshot.html"
            shot_page.write_text(with_font_link(doc, a.font_account))
        desktop = out / "klaviyo-desktop.png"
        screenshot(chrome_bin, shot_page, desktop, 600, a.height)
        wrap = out / "mobile-frame.html"
        wrap.write_text(f'<!doctype html><html><body style="margin:0"><iframe src="{shot_page.name}" '
                        f'style="width:375px;height:{a.height * 2}px;border:0;display:block"></iframe></body></html>')
        mobile_full = out / "mobile-wrap.png"
        screenshot(chrome_bin, wrap, mobile_full, 600, a.height * 2)
        from PIL import Image, ImageChops

        def trim(img):
            """Cut trailing rows that are just page background (the screenshot is taller than the email)."""
            bg = Image.new("RGB", img.size, img.getpixel((img.width - 1, img.height - 1)))
            box = ImageChops.difference(img, bg).getbbox()
            return img.crop((0, 0, img.width, min(img.height, box[3] + 24))) if box else img
        mob = trim(Image.open(mobile_full).convert("RGB").crop((0, 0, 375, a.height * 2)))
        mob.save(out / "klaviyo-mobile.png")
        mobile_full.unlink()
        desk = trim(Image.open(desktop).convert("RGB"))
        desk.save(desktop)
        panels = []
        if a.figma_render:
            panels.append(Image.open(a.figma_render).convert("RGB"))
        panels += [desk, mob]
        h = max(pn.height for pn in panels)
        board = Image.new("RGB", (sum(pn.width for pn in panels) + 30 * (len(panels) - 1), h), "white")
        x = 0
        for pn in panels:
            board.paste(pn, (x, 0))
            x += pn.width + 30
        board.save(out / "compare.png")
        report["screenshots"] = ["klaviyo-desktop.png", "klaviyo-mobile.png", "compare.png"]
    else:
        report["screenshots"] = "Chrome not found; screenshot rendered.html yourself (mobile needs a <480px viewport)"
    (out / "check.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps(report, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="frame node tree + Figma render")
    s.add_argument("--figma-slug", required=True)
    s.add_argument("--file", required=True, help="Figma file key")
    s.add_argument("--frame", required=True, help="frame node id, e.g. 1:0")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("outline", help="print the layer outline")
    s.add_argument("--tree", required=True)
    s.add_argument("--frame")
    s.set_defaults(fn=cmd_outline)

    s = sub.add_parser("export", help="manual: export text-free nodes as images (refuses live text)")
    s.add_argument("--tree", required=True, help="Figma node_tree.json, used to refuse text-bearing nodes")
    s.add_argument("--figma-slug", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--node", action="append", required=True, metavar="ID=NAME")
    s.add_argument("--scale", type=int, default=2)
    s.add_argument("--jpeg", help="comma-separated names to save as JPEG (photos)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("assets", help="export, crop, and composite the images an IR needs")
    s.add_argument("--tree", required=True, help="Figma node_tree.json")
    s.add_argument("--frame", help="frame node id (default: first tree key, lexically)")
    s.add_argument("--figma-slug", required=True)
    s.add_argument("--file", required=True, help="Figma file key")
    s.add_argument("--out", required=True, help="folder for image files and asset_plan.json")
    s.add_argument("--scale", type=int, default=2)
    s.add_argument("--only", help="comma-separated node ids to (re-)export")
    s.add_argument("--overrides", help="the same overrides JSON as convert, so skipped images are not exported")
    s.set_defaults(fn=cmd_assets)

    s = sub.add_parser("convert", help="convert a Figma tree into an editable template draft")
    s.add_argument("--tree", required=True, help="Figma node_tree.json")
    s.add_argument("--frame", help="frame node id (default: first tree key, lexically)")
    s.add_argument("--brand", help="brand JSON: fonts, mobile sizes, colors, footer")
    s.add_argument("--overrides", help="JSON: links, alt, text, roles, skip")
    s.add_argument("--assets", help="JSON mapping node ids to uploaded asset id/url")
    s.add_argument("--out", required=True, help="output folder for template, decisions, TODOs, overrides worksheet")
    s.add_argument("--name", help="template name (default: Figma frame name)")
    s.set_defaults(fn=cmd_convert)

    s = sub.add_parser("fonts", help="design fonts vs Klaviyo-registered fonts")
    s.add_argument("--tree", required=True)
    s.add_argument("--company-id", help="Klaviyo account id (public custom_fonts.css)")
    s.add_argument("--brand", help="brand JSON mapping Figma families to registered Klaviyo names")
    s.set_defaults(fn=cmd_fonts)

    s = sub.add_parser("upload", help="upload images to Klaviyo")
    s.add_argument("--slug", required=True)
    s.add_argument("--out", help="folder for uploads.json")
    s.add_argument("--image", action="append", metavar="NAME=PATH")
    s.add_argument("--plan", help="asset_plan.json from `assets`: upload planned files, write assets.json beside it")
    s.add_argument("--force", action="store_true", help="with --plan, re-upload node ids already in assets.json")
    s.add_argument("--prefix", default="", help="name prefix, e.g. a run tag")
    s.add_argument("--visible", action="store_true", help="don't mark hidden")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("validate", help="schema-check a template body and reject TODOs/placeholders")
    s.add_argument("--body", required=True)
    s.add_argument("--allow-todo", action="store_true",
                   help="allow TODO markers and placeholder links in drafts; still require valid schema")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("push", help="create or update the template")
    s.add_argument("--slug", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--template-id", help="update this template instead of creating one")
    s.add_argument("--expect-account", help="refuse unless /accounts returns this id")
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("check", help="render, screenshot, and compare")
    s.add_argument("--slug", required=True)
    s.add_argument("--template-id", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--tree", help="node_tree.json, for the text check")
    s.add_argument("--figma-render", help="Figma's render PNG, for the board")
    s.add_argument("--context", help="JSON render context (default: empty, which shows fallbacks)")
    s.add_argument("--height", type=int, default=4000, help="desktop screenshot height")
    s.add_argument("--font-account", help="Klaviyo company id whose registered brand fonts are injected into the screenshot copy")
    s.set_defaults(fn=cmd_check)

    a = ap.parse_args()
    try:
        a.fn(a)
    except LoggieError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
