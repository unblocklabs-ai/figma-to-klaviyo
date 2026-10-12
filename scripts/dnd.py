"""Builders for Klaviyo SYSTEM_DRAGGABLE template definitions from a Figma node tree.

Import this from a per-email build script. Every block is live and editable:
text stays text, buttons stay buttons, and images carry only photography, illustration, logos,
and icons.

Rendering rules these helpers already apply (verified in Klaviyo, 2026-09-30):
  - every <p> gets margin:0 and padding-bottom:0, because Klaviyo's CSS adds padding-bottom:1em;
  - type roles go into heading/text styles with mobile sizes, since inline sizes don't scale on phones;
  - button letter_spacing is rounded to whole pixels, the only thing Klaviyo accepts.
"""
from __future__ import annotations

import html
import json
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import TypeVar

# ---------------------------------------------------------------------------
# Figma tree access
# ---------------------------------------------------------------------------


class Frame:
    """A Figma frame from GET /v1/files/{key}/nodes?ids={id}. Hidden layers are ignored."""

    def __init__(self, tree_path: str | Path, frame_id: str | None = None):
        nodes = json.loads(Path(tree_path).read_text())["nodes"]
        frame_id = frame_id or next(iter(nodes))
        self.root = nodes[frame_id]["document"]
        self.origin = self.root["absoluteBoundingBox"]

    def walk(self, node=None):
        node = node or self.root
        if node.get("visible", True) is False:
            return
        yield node
        for child in node.get("children") or []:
            yield from self.walk(child)

    def find(self, name: str, within=None):
        """First visible node with exactly this name."""
        return next((n for n in self.walk(within) if n.get("name") == name), None)

    def by_id(self, node_id: str):
        return next((n for n in self.walk() if n["id"] == node_id), None)

    def texts(self, within=None):
        return [n for n in self.walk(within) if n["type"] == "TEXT"]

    def box(self, node):
        """(x, y, width, height) relative to the frame's top-left corner."""
        b = node["absoluteBoundingBox"]
        return (round(b["x"] - self.origin["x"]), round(b["y"] - self.origin["y"]),
                round(b["width"]), round(b["height"]))


def hexcolor(node, kind: str = "fills") -> str | None:
    """Hex of the first visible solid fill (or stroke)."""
    for paint in node.get(kind) or []:
        if paint.get("visible", True) and paint["type"] == "SOLID":
            r, g, b = (round(paint["color"][k] * 255) for k in "rgb")
            return f"#{r:02X}{g:02X}{b:02X}"
    return None


def has_image_fill(node) -> bool:
    return any(p.get("type") == "IMAGE" and p.get("visible", True) for p in node.get("fills") or [])


def label(text_node) -> str:
    """Text as Figma displays it, applying textCase UPPER/LOWER."""
    chars = text_node.get("characters", "")
    case = text_node.get("style", {}).get("textCase")
    return chars.upper() if case == "UPPER" else chars.lower() if case == "LOWER" else chars


def padding(node) -> tuple[int, int, int, int]:
    """Auto-layout padding as (top, right, bottom, left)."""
    return (int(node.get("paddingTop", 0)), int(node.get("paddingRight", 0)),
            int(node.get("paddingBottom", 0)), int(node.get("paddingLeft", 0)))


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

COLUMN_PRESETS: dict[int, dict[str, tuple[int, ...]]] = {
    1: {"1-column-full-width": (100,)},
    2: {"2-columns-equal-width": (50, 50), "2-columns-25%-75%": (25, 75), "2-columns-33%-67%": (33, 67),
        "2-columns-67%-33%": (67, 33), "2-columns-75%-25%": (75, 25)},
    3: {"3-columns-equal-width": (33, 33, 33), "3-columns-25%-25%-50%": (25, 25, 50),
        "3-columns-25%-50%-25%": (25, 50, 25), "3-columns-50%-25%-25%": (50, 25, 25)},
    4: {"4-columns-equal-width": (25, 25, 25, 25)},
}


def split_widths(frame: Frame, members: list, left: float, right: float) -> list[float]:
    """Column widths for side-by-side nodes, splitting at the middle of each gap between them.
    left/right are the content edges (frame x minus the section's side padding)."""
    return split_widths_by_box([frame.box(n) for n in members], left, right)


def split_widths_by_box(boxes: list[tuple[float, float, float, float]],
                        left: float, right: float) -> list[float]:
    """Geometry-only variant of split_widths; boxes are (x, y, width, height)."""
    boxes = sorted(boxes)
    edges = [left]
    for (x1, _, w1, _), (x2, _, _, _) in pairwise(boxes):
        edges.append((x1 + w1 + x2) / 2)
    edges.append(right)
    return [b - a for a, b in pairwise(edges)]


def nearest_column_layout(widths: list[float], min_widths: list[float] | None = None) -> str:
    """Closest Klaviyo column preset for these column widths (px). min_widths lists fixed-size
    content per column (e.g. an image's width); presets whose column would be narrower are
    skipped so the content isn't squeezed. Free ratios need a split block instead."""
    total = sum(widths)
    shares = [100 * w / total for w in widths]
    presets = COLUMN_PRESETS[len(widths)]
    fits = [name for name in presets
            if not min_widths or all(total * s / 100 >= m for s, m in zip(presets[name], min_widths))]
    candidates = fits or list(presets)
    return min(candidates, key=lambda name: sum(abs(a - b) for a, b in zip(shares, presets[name])))


T = TypeVar("T")


def group_rows_by_box(items: list[T], box: Callable[[T], tuple[float, float, float, float]],
                      gap: int = 1) -> list[list[T]]:
    """Geometry-only row grouping, shared by Figma nodes and intermediate elements."""
    ordered = sorted(items, key=lambda item: box(item)[1])
    rows: list[list[T]] = []
    for item in ordered:
        _, y, _, h = box(item)
        if rows:
            top = min(box(member)[1] for member in rows[-1])
            bottom = max(box(member)[1] + box(member)[3] for member in rows[-1])
            if y < bottom - gap and y + h > top + gap:
                rows[-1].append(item)
                continue
        rows.append([item])
    return [sorted(row, key=lambda item: box(item)[0]) for row in rows]


def group_rows(frame: Frame, nodes: list, gap: int = 1) -> list[list]:
    """Group absolutely positioned nodes into rows by vertical overlap, top to bottom,
    each row sorted left to right. Use this when a Figma band isn't auto-layout."""
    return group_rows_by_box(nodes, frame.box, gap)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def pads(top=0, right=0, bottom=0, left=0) -> dict:
    return {"block_padding_top": top, "block_padding_right": right,
            "block_padding_bottom": bottom, "block_padding_left": left}


def p(text: str, style: str = "") -> str:
    """A paragraph with Klaviyo's default paragraph padding removed."""
    extra = f";{style}" if style else ""
    return f'<p style="margin:0;padding-bottom:0{extra}">{html.escape(text, quote=False)}</p>'


def h(level: int, text: str, style: str = "") -> str:
    """A heading; its look comes from heading-{level}-styles, including the mobile size."""
    attr = f' style="{style}"' if style else ""
    return f"<h{level}{attr}>{html.escape(text, quote=False)}</h{level}>"


def text(content_html: str, **pad) -> dict:
    return {"content_type": "block", "type": "text",
            "data": {"content": content_html, "display_options": {}, "styles": pads(**pad)}}


def image(asset: dict, alt: str, width: int, align: str = "center", href: str | None = None,
          max_width: int | None = None, full_width_mobile: bool = False, **pad) -> dict:
    """asset is {"id": ..., "url": ...} from an upload. alt is required: describe the picture."""
    if not alt:
        raise ValueError("image blocks need alt text")
    props = {"dynamic": False, "asset_id": str(asset["id"]), "src": asset["url"], "alt_text": alt}
    if href:
        props["href"] = href
    styles = {"width": width, "align": align, "full_width_mobile": full_width_mobile, **pads(**pad)}
    if max_width:
        styles["max_width"] = max_width
    return {"content_type": "block", "type": "image",
            "data": {"properties": props, "display_options": {}, "styles": styles}}


def button(label_text: str, href: str, *, bg: str | None, color: str | None, font_family: str, font_size: int,
           font_weight: str, letter_spacing: float = 0, inner=(16, 32, 16, 32), align: str = "center",
           radius: int = 0, border: tuple[str, float] | None = None, **pad) -> dict:
    top, right, bottom, left = inner
    border_styles = ({"border_color": border[0], "border_width": round(border[1]),
                      "border_style": "solid"} if border else {})
    return {"content_type": "block", "type": "button",
            "data": {"content": label_text, "properties": {"href": href}, "display_options": {},
                     "styles": {"background_color": bg, "color": color, "font_family": font_family,
                                "font_size": int(font_size), "font_weight": str(font_weight),
                                "letter_spacing": round(letter_spacing),  # whole pixels only
                                "border_radius": radius, "text_align": align,
                                "inner_padding_top": top, "inner_padding_right": right,
                                "inner_padding_bottom": bottom, "inner_padding_left": left,
                                **border_styles, **pads(**pad)}}}


def button_from_figma(frame: Frame, instance, href: str, font_family: str, align: str, **pad) -> dict:
    """A Figma button (auto-layout frame or instance with one text child) as a Klaviyo button."""
    lbl = next(n for n in frame.walk(instance) if n["type"] == "TEXT")
    s = lbl["style"]
    return button(label(lbl), href, bg=hexcolor(instance), color=hexcolor(lbl), font_family=font_family,
                  font_size=s["fontSize"], font_weight=str(s["fontWeight"]),
                  letter_spacing=s.get("letterSpacing", 0), inner=padding(instance),
                  align=align, radius=int(instance.get("cornerRadius", 0)), **pad)


def divider(color: str = "#000000", width: int = 1, style: str = "solid", **pad) -> dict:
    return {"content_type": "block", "type": "horizontal_rule",
            "data": {"display_options": {}, "styles": {"border_color": color, "border_style": style,
                                                         "border_width": width, **pads(**pad)}}}


def spacer(height: int, color: str | None = None) -> dict:
    styles: dict[str, int | str] = {"height": height}
    if color:
        styles["background_color"] = color
    return {"content_type": "block", "type": "spacer", "data": {"display_options": {}, "styles": styles}}


def row(layout: str, *columns: list) -> dict:
    return {"data": {"styles": {"column_layout": layout}},
            "columns": [{"data": {}, "blocks": list(c)} for c in columns]}


def section(rows: list, *, bg: str, pad=(0, 0, 0, 0), column_align: str | None = None,
            column_direction: str | None = None) -> dict:
    """column_direction changes only the MOBILE stacking order: "rtl" stacks columns in reverse,
    "no-stack" keeps them side by side. Desktop always follows source order."""
    top, right, bottom, left = pad
    styles = {"background_color": bg, "inner_padding_top": top, "inner_padding_right": right,
              "inner_padding_bottom": bottom, "inner_padding_left": left}
    if column_align:
        styles["column_align"] = column_align
    if column_direction:
        styles["column_direction"] = column_direction
    return {"content_type": "section", "type": "section",
            "data": {"properties": {}, "display_options": {}, "styles": styles}, "rows": rows}


# ---------------------------------------------------------------------------
# Template styles
# ---------------------------------------------------------------------------


def type_role(node, font_family: str, mobile_px: int, **overrides) -> dict:
    """Style values for a heading or text role, taken from a Figma TEXT node."""
    s = node["style"]
    styles = type_role_values(font_family, s["fontSize"], s["fontWeight"], mobile_px,
                               line_height_px=s.get("lineHeightPx"), italic=bool(s.get("italic")),
                               letter_spacing=s.get("letterSpacing", 0), color=hexcolor(node) or "#000000",
                               align=(s.get("textAlignHorizontal") or "LEFT").lower())
    styles.update(overrides)
    return styles


def type_role_values(font_family: str, size: float, weight: str | float, mobile_px: int, *,
                     line_height_px: float | None = None, italic: bool = False,
                     letter_spacing: float = 0, color: str = "#000000", align: str = "left",
                     **overrides) -> dict:
    """A type role from plain values, with the same contract as type_role."""
    ratio = round(line_height_px / size, 2) if line_height_px else 1.2
    styles = {"font_family": font_family, "font_size": int(size), "font_weight": str(weight),
              "font_style": "italic" if italic else "normal", "line_height": ratio,
              "letter_spacing": round(letter_spacing), "color": color, "text_align": align,
              "mobile_font_size": mobile_px, "mobile_line_height": ratio}
    styles.update(overrides)
    return styles


def template_styles(*, background: str, text: dict, headings: dict[int, dict] | None = None,
                    link: dict | None = None, margin_top: int = 0) -> list:
    """The 8 required style entries. headings maps level (1-4) to type_role(...) output;
    heading margins default to 0 so spacing is set explicitly in the layout."""
    headings = headings or {}
    out = [{"style_type": "base-styles", "properties": {"mobile_optimizations": True},
            "styles": {"content_background_color": background, "margin_top": margin_top}},
           {"style_type": "text-styles", "styles": text},
           {"style_type": "link-styles", "styles": link or {"color": text.get("color", "#000000"),
                                                            "text_decoration": "underline"}}]
    for level in (1, 2, 3, 4):
        style = dict(headings.get(level, {}))
        if style:
            style.setdefault("margin_bottom", 0)
        out.append({"style_type": f"heading-{level}-styles", "styles": style})
    out.append({"style_type": "mobile-styles", "properties": {}, "styles": {}})
    return out


def template_body(name: str, sections: list, styles: list, background: str, width: int = 600) -> dict:
    return {"data": {"type": "template", "attributes": {
        "name": name, "editor_type": "SYSTEM_DRAGGABLE",
        "definition": {"styles": styles,
                       "body": {"properties": {}, "styles": {"background_color": background, "width": width},
                                "sections": sections}}}}}


def footer(font_family: str, bg: str, color: str = "#555555", pad=(8, 40, 32, 40)) -> dict:
    """Minimal compliant footer: organization details, unsubscribe, manage preferences.
    Prefer the brand's own footer (its base template) when the brand has one."""
    content = (f'<p style="margin:0;padding-bottom:0;font-family:{font_family};font-size:11px;'
               f'line-height:16px;color:{color};text-align:center">'
               "{{ organization.name }} · {{ organization.full_address }}<br>"
               "{% unsubscribe 'Unsubscribe' %} · {% manage_preferences 'Manage preferences' %}</p>")
    return section([row("1-column-full-width", [text(content)])], bg=bg, pad=pad)
