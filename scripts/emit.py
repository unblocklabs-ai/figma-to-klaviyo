"""Deterministic, offline IR → native Klaviyo drag-and-drop template bodies.

Missing brief inputs stay visible as TODO markers. Mobile settings accept role
sizes (h1–h4, text) and display_factor/heading_factor/body_factor. Text overrides
use Text node ids, including a button's label id. No input is mutated.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from typing import TypeVar

import dnd
from ir import (
    IR,
    Band,
    Box,
    Button,
    Card,
    Cell,
    Confidence,
    Divider,
    Element,
    Image,
    Row,
    Text,
    TextStyle,
    describe_stroke,
    envelope,
    is_quarter_turn,
    visible_box,
)


@dataclass
class Conversion:
    body: dict
    decisions: list[dict]
    todos: list[str]


def find_todos(body: object) -> list[str]:
    """Find decoded markers in every string value (and key), including nested properties."""
    from urllib.parse import unquote

    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, str):
            decoded = html.unescape(unquote(html.unescape(value)))
            found.update(re.findall(r"TODO:[\w-]+:[^\s<>\"'&]+", decoded))
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(key)
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(body)
    return sorted(found)


RoleKey = tuple[str | None, int, float, bool]
T = TypeVar("T")
FALLBACKS = {"serif": "Georgia, 'Times New Roman', serif",
             "sans": "'Helvetica Neue', Helvetica, Arial, sans-serif"}


def _mode(values: list[T]) -> T:
    # Equal counts keep source reading order, independent of dict/hash ordering.
    counts = Counter(values)
    return max(values, key=lambda value: counts[value])


def _key(style: TextStyle) -> RoleKey:
    return (style.family, round(style.size if style.size is not None else 16),
            style.weight if style.weight is not None else 400, style.italic)


def _bottom(box: Box) -> float:
    return box.y + box.h


def _escaped(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br>").replace("\u2028", "<br>")


CARD_LABEL_GAP = 16


def _own_content_color(section: dict) -> dict:
    """Klaviyo paints a section color only on the full-width outer table; the 600 px column
    uses the template content color unless the section opts in to its own."""
    styles = section["data"]["styles"]
    styles["content_color"] = styles["background_color"]
    styles["content_color_type"] = "section"
    return section


def _css(properties: dict[str, str]) -> str:
    return ";".join(f"{key}:{value}" for key, value in properties.items())


@dataclass
class _Role:
    key: RoleKey
    style: TextStyle
    values: dict


class _Emitter:
    def __init__(self, ir: IR, brand: dict, overrides: dict, assets: dict):
        self.ir, self.brand, self.overrides, self.assets = ir, brand, overrides, assets
        self.decisions = [asdict(decision) for decision in ir.decisions]
        self.skip = set(overrides.get("skip", []))
        self.skipped: set[str] = set()
        self.stacks: dict[str | None, str] = {}
        self.roles: dict[str, _Role] = {}
        self.role_for_key: dict[RoleKey, str] = {}
        self.group_for_key: dict[RoleKey, RoleKey] = {}

    def decide(self, node_id: str, rule: str, detail: str, confidence: Confidence = "high") -> None:
        self.decisions.append({"node_id": node_id, "rule": rule, "detail": detail, "confidence": confidence})

    def drop(self, node_id: str) -> bool:
        if node_id not in self.skip:
            return False
        if node_id not in self.skipped:
            self.decide(node_id, "skipped-override", "Dropped by overrides.skip.")
            self.skipped.add(node_id)
        return True

    def override_text(self, text: Text) -> Text:
        replacements = self.overrides.get("text", {})
        if text.node_id not in replacements:
            return text
        self.decide(text.node_id, "text-override", "Replaced characters; discarded obsolete style runs.")
        return replace(text, characters=replacements[text.node_id], segments=[])

    def prepare(self) -> list[Band]:
        bands = []
        for band in self.ir.bands:
            if self.drop(band.node_id):
                continue
            rows = []
            for row in band.rows:
                cells = []
                for cell in row.cells:
                    elements: list[Element] = []
                    for element in cell.elements:
                        if self.drop(element.node_id):
                            continue
                        if isinstance(element, Divider) and element.orientation == "vertical":
                            continue
                        if isinstance(element, Image):
                            box = visible_box(element)
                            if box.w <= 0 or box.h <= 0:
                                self.decide(element.node_id, "offcanvas-image",
                                            "Visible image box has no area; omitted.", "low")
                                continue
                        if isinstance(element, Text):
                            element = self.override_text(element)
                        elif isinstance(element, Button):
                            if self.drop(element.label.node_id):
                                continue
                            element = replace(element, label=self.override_text(element.label))
                        elements.append(element)
                    if elements:
                        cells.append(Cell(envelope([visible_box(e) for e in elements]), elements))
                if row.card is not None:
                    rows.extend(self.card_rows(row.card, cells))
                elif cells:
                    rows.append(Row(envelope([c.box for c in cells]), cells))
            rows = self.merge_rows(band, rows)
            limited = []
            for row in rows:
                if len(row.cells) > 4:
                    dropped = [e.node_id for c in row.cells[4:] for e in c.elements]
                    self.decide(band.node_id, "columns-overflow",
                                f"Kept first four columns; omitted {', '.join(dropped)}.", "low")
                    row = replace(row, box=envelope([c.box for c in row.cells[:4]]), cells=row.cells[:4])
                limited.append(row)
            background = band.background_image
            if background and self.drop(background.node_id):
                background = None
            bands.append(replace(band, rows=limited, background_image=background))
        return bands

    def card_rows(self, card: Card, cells: list[Cell]) -> list[Row]:
        """Hoist quarter-turn labels into full-width heading rows ahead of the card's content cells."""
        labels = [e for c in cells for e in c.elements if isinstance(e, Text) and is_quarter_turn(e)]
        rows: list[Row] = []
        y = card.box.y
        for label in labels:
            align = self.alignment(label.box, card.box.x, card.box.w)
            box = Box(card.box.x, y, card.box.w, label.box.w)
            horizontal = replace(label, box=box, rotation_deg=0, style=replace(label.style, align=align))
            self.decide(label.node_id, "card-label-hoisted",
                        f"Quarter-turn label moved to a full-width {align}-aligned heading row; "
                        "rotation is not reproduced.", "medium")
            rows.append(Row(box, [Cell(box, [horizontal])], card))
            y += box.h
        cells = [Cell(envelope([visible_box(e) for e in kept]), kept)
                 for c in cells if (kept := [e for e in c.elements if e not in labels])]
        if cells:
            rows.append(Row(envelope([c.box for c in cells]), cells, card))
        return rows

    def merge_rows(self, band: Band, rows: list[Row]) -> list[Row]:
        merged: list[Row] = []
        for row in rows:
            previous = merged[-1] if merged else None
            if row.card is not None or (previous is not None and previous.card is not None):
                merged.append(row)
                continue
            aligned = previous is not None and len(row.cells) > 1 and len(row.cells) == len(previous.cells)
            if aligned and previous is not None:
                aligned = all(min(a.box.x + a.box.w, b.box.x + b.box.w) > max(a.box.x, b.box.x)
                              for a, b in zip(previous.cells, row.cells))
            if not aligned or previous is None:
                merged.append(row)
                continue
            old_images = [any(isinstance(e, Image) for e in c.elements) for c in previous.cells]
            new_images = [any(isinstance(e, Image) for e in c.elements) for c in row.cells]
            if any(old_images) and any(new_images) and old_images != new_images:
                # Pairwise overlap alone would merge alternating photo/caption rows.
                ids = [e.node_id for r in (previous, row) for c in r.cells for e in c.elements]
                self.decide(band.node_id, "row-merge-deferred",
                            f"Kept alternating image/text lanes separate: {', '.join(ids)}.", "medium")
                merged.append(row)
                continue
            cells = [Cell(envelope([a.box, b.box]), [*a.elements, *b.elements])
                     for a, b in zip(previous.cells, row.cells)]
            merged[-1] = Row(envelope([c.box for c in cells]), cells)
            ids = [e.node_id for c in cells for e in c.elements]
            self.decide(band.node_id, "row-merge", f"Merged aligned column tracks: {', '.join(ids)}.")
        return merged

    def stack(self, family: str | None) -> str:
        if family not in self.stacks:
            mapping = self.brand.get("font_map", {}).get(family, {})
            stack = mapping.get("stack")
            if not stack:
                kind = "serif" if any(s in (family or "").lower() for s in ("gestura", "serif")) else "sans"
                stack = self.brand.get("fallback_stacks", {}).get(kind, FALLBACKS[kind])
                self.decide(self.ir.frame_id, "font-unmapped",
                            f"{family or '(unspecified)'} uses fallback {stack}.", "medium")
            self.stacks[family] = stack
        return self.stacks[family]

    def mobile_size(self, size: int, role: str) -> int:
        mobile = self.brand.get("mobile", {})
        if role in mobile:
            return round(mobile[role])
        if size >= 40:
            return round(size * mobile.get("display_factor", 0.65))
        if size > 22:
            return round(size * mobile.get("heading_factor", 0.8))
        return max(16 if role == "text" else 12, round(size * mobile.get("body_factor", 0.9)))

    def make_role(self, key: RoleKey, texts: list[Text], role: str) -> _Role:
        style = replace(texts[0].style, family=key[0], size=key[1], weight=key[2], italic=key[3],
                        lineHeightPx=_mode([t.style.lineHeightPx for t in texts]),
                        letterSpacing=_mode([t.style.letterSpacing for t in texts]),
                        color=_mode([t.style.color for t in texts]), align=_mode([t.style.align for t in texts]))
        values = dnd.type_role_values(self.stack(key[0]), key[1], f"{key[2]:g}",
                                      self.mobile_size(key[1], role), line_height_px=style.lineHeightPx,
                                      italic=key[3], letter_spacing=style.letterSpacing,
                                      color=style.color or "#000000",
                                      align="left" if style.align == "justified" else style.align)
        return _Role(key, style, values)

    def build_roles(self, bands: list[Band]) -> None:
        texts = [e for b in bands for r in b.rows for c in r.cells for e in c.elements if isinstance(e, Text)]
        labels = [e.label for b in bands for r in b.rows for c in r.cells for e in c.elements
                  if isinstance(e, Button)]
        italic_ids: dict[str | None, list[str]] = {}
        for text in [*texts, *labels]:
            for style in [text.style, *(s.style for s in text.segments)]:
                self.stack(style.family)
                if style.italic:
                    ids = italic_ids.setdefault(style.family, [])
                    if text.node_id not in ids:
                        ids.append(text.node_id)
        for family, ids in italic_ids.items():
            if self.brand.get("font_map", {}).get(family, {}).get("italic") is False:
                self.decide(ids[0], "font-italic-unavailable",
                            f"{family}: kept italic; unavailable for nodes {', '.join(ids)}.", "medium")

        groups: dict[RoleKey, list[Text]] = {}
        for text in texts:
            groups.setdefault(_key(text.style), []).append(text)
        if not groups:
            key: RoleKey = (None, 16, 400, False)
            style = TextStyle(None, None, 16, None, 400, False, 0, "left", "#000000")
            groups[key] = [Text(self.ir.frame_id, Box(0, 0, 0, 0), "", style, [])]

        small = [key for key in groups if key[1] <= 22]
        candidates = small or [key for key in groups if key[1] == min(k[1] for k in groups)]
        body = min(candidates, key=lambda key: (-sum(len(t.characters) for t in groups[key]),
                                                -key[1], key[0] or "", key[2], key[3]))
        self.roles["text"] = self.make_role(body, groups[body], "text")
        self.role_for_key[body] = "text"

        heading_groups: dict[RoleKey, list[Text]] = {}
        for key in sorted((k for k in groups if k[1] > 22),
                          key=lambda k: (-k[1], k[0] or "", k[2], k[3])):
            target = next((k for k in heading_groups if k[0] == key[0] and k[2:] == key[2:]
                           and k[1] - key[1] <= 2), key)
            heading_groups.setdefault(target, []).extend(groups[key])
            self.group_for_key[key] = target
        for level, (key, members) in enumerate(heading_groups.items(), 1):
            role = f"h{level}" if level <= 4 else "inline"
            for original, target in self.group_for_key.items():
                if target == key:
                    self.role_for_key[original] = role
            if level <= 4:
                self.roles[role] = self.make_role(key, members, role)
            else:
                for text in members:
                    self.decide(text.node_id, "heading-overflow",
                                f"{key[1]} px exceeds four heading roles; kept as an inline paragraph.", "medium")
        # A body selected from display-only type remains a paragraph, not a heading.
        self.role_for_key[body] = "text"
        for text in texts:
            forced = self.overrides.get("roles", {}).get(text.node_id)
            if forced in {"h1", "h2", "h3", "h4"} and forced not in self.roles:
                self.roles[forced] = self.make_role(_key(text.style), groups[_key(text.style)], forced)

    def full_text_css(self, style: TextStyle, size: int) -> dict[str, str]:
        ratio = round(style.lineHeightPx / size, 2) if style.lineHeightPx else 1.2
        return {"font-family": self.stack(style.family), "font-size": f"{size}px",
                "font-weight": f"{style.weight if style.weight is not None else 400:g}",
                "font-style": "italic" if style.italic else "normal", "line-height": f"{ratio:g}",
                "letter-spacing": f"{style.letterSpacing:g}px", "color": style.color or "#000000"}

    def text_html(self, text: Text, left: float, width: float, *, single_cell: bool) -> str:
        key = _key(text.style)
        forced = self.overrides.get("roles", {}).get(text.node_id)
        role = forced if forced in {"text", "h1", "h2", "h3", "h4"} else self.role_for_key.get(key, "inline")
        if forced in {"text", "h1", "h2", "h3", "h4"}:
            self.decide(text.node_id, "role-override", f"Forced {role}; retained Figma style differences inline.")
        target = self.roles.get(role)
        css: dict[str, str] = {}
        if target is None or (forced and target.key != self.group_for_key.get(key, key)):
            css = self.full_text_css(text.style, key[1])
        else:
            own = self.full_text_css(text.style, target.key[1])
            for prop, field in (("line-height", "lineHeightPx"), ("letter-spacing", "letterSpacing"),
                                ("color", "color")):
                if getattr(text.style, field) != getattr(target.style, field):
                    css[prop] = own[prop]
        alignment = (target or self.roles["text"]).values["text_align"]
        if text.style.align == "justified":
            css["text-align"] = "justify"
        elif text.style.align != alignment:
            css["text-align"] = text.style.align
        if "font-size" in css and key[1] > 16:
            self.decide(text.node_id, "inline-size", f"Inline {key[1]} px does not scale on mobile.", "medium")

        quarter_turn = is_quarter_turn(text)
        if single_cell and not quarter_turn and text.box.w + 8 < width:
            css["max-width"] = f"{text.box.w + 8:g}px"
            left_inset, right_inset = text.box.x - left, left + width - text.box.x - text.box.w
            if abs(left_inset - right_inset) <= 12:
                css.update({"margin-left": "auto", "margin-right": "auto"})
            elif right_inset < left_inset:
                css["margin-left"] = "auto"

        markup = _escaped(text.characters)
        if text.segments:
            runs = []
            for segment in text.segments:
                span = {}
                for field, prop in (("weight", "font-weight"), ("italic", "font-style"),
                                    ("color", "color"), ("size", "font-size")):
                    value = getattr(segment.style, field)
                    if value == getattr(text.style, field):
                        continue
                    if field == "italic":
                        span[prop] = "italic" if value else "normal"
                    elif field == "size" and value is not None:
                        span[prop] = f"{value:g}px"
                    elif field == "weight" and value is not None:
                        span[prop] = f"{value:g}"
                    elif field == "color":
                        span[prop] = value or "#000000"
                chars = _escaped(segment.text)
                runs.append(f'<span style="{html.escape(_css(span), quote=True)}">{chars}</span>' if span else chars)
            markup = "".join(runs)
        shell = dnd.h(int(role[1]), "", html.escape(_css(css), quote=True)) if role.startswith("h") else dnd.p(
            "", html.escape(_css(css), quote=True))
        opening, closing = shell.split(">", 1)
        return f"{opening}>{markup}{closing}"

    def asset(self, node_id: str) -> dict:
        asset = self.assets.get(node_id)
        if asset and asset.get("id") is not None and asset.get("url"):
            return asset
        return {"id": f"TODO:asset:{node_id}", "url": f"https://todo.invalid/asset/{node_id}"}

    @staticmethod
    def alignment(box: Box, left: float, width: float) -> str:
        delta = box.x + box.w / 2 - (left + width / 2)
        return "center" if abs(delta) <= 12 else "left" if delta < 0 else "right"

    def block(self, element: Element, left: float, width: float, bg: str, top: int, *, single_cell: bool) -> dict:
        if isinstance(element, Text):
            return dnd.text(self.text_html(element, left, width, single_cell=single_cell), top=top)
        if isinstance(element, Button):
            style = element.label.style
            t, r, b, l = element.padding
            fixed = (element.box.w - element.label.box.w) / 2
            href = self.overrides.get("links", {}).get(element.node_id) or f"TODO:link:{element.node_id}"
            border = (element.stroke.color, element.stroke.weight) if element.stroke else None
            return dnd.button(_escaped(element.label.characters), href, bg=element.fill or bg,
                              color=style.color or "#000000", font_family=self.stack(style.family),
                              font_size=round(style.size if style.size is not None else 16),
                              font_weight=f"{style.weight if style.weight is not None else 400:g}",
                              letter_spacing=style.letterSpacing,
                              inner=(round(t), round(max(r, fixed)), round(b), round(max(l, fixed))),
                              align=self.alignment(element.box, left, width), radius=round(element.corner_radius),
                              border=border, top=top)
        if isinstance(element, Image):
            box = visible_box(element)
            size = min(round(box.w), round(width))
            return dnd.image(self.asset(element.node_id),
                             self.overrides.get("alt", {}).get(element.node_id) or f"TODO:alt:{element.node_id}",
                             size, self.alignment(box, left, width),
                             href=self.overrides.get("links", {}).get(element.node_id),
                             max_width=size if element.vector else None, top=top)
        if isinstance(element, Divider):
            return dnd.divider(element.color or "#000000", max(1, round(element.weight)), top=top)
        return dnd.text(dnd.p(f"TODO:unmapped:{element.node_id} {element.node_type}"), top=top)

    def gap(self, value: float, node_id: str, detail: str) -> int:
        if value < -0.5:
            self.decide(node_id, "overlap-gap", f"{detail}: {value:g} px; clamped to 0 to keep live blocks.", "low")
        return max(0, round(value))

    @staticmethod
    def row_options(row: Row) -> tuple[str, str]:
        centers = [c.box.y + c.box.h / 2 for c in row.cells]
        align = "middle" if max(centers) - min(centers) <= 12 else "top"
        images = [any(isinstance(e, Image) for e in c.elements) for c in row.cells]
        return align, "rtl" if not images[0] and any(images[1:]) else "ltr"

    def gutter(self) -> float:
        value = self.brand.get("gutter", 40)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            self.decide(self.ir.frame_id, "gutter-fallback",
                        f"Brand gutter {value!r} is not a non-negative number; used 40.", "low")
            return 40
        return value

    def sections(self, band: Band, page_bg: str) -> list[dict]:
        plain = [e for r in band.rows if r.card is None for c in r.cells for e in c.elements]
        elements = plain or [e for r in band.rows for c in r.cells for e in c.elements]
        gutter = self.gutter()
        if any(band.padding):
            right_pad, left_pad = round(band.padding[1]), round(band.padding[3])
        else:
            # Klaviyo keeps section padding on phones, so a large free-position inset would
            # squeeze mobile text; cap it at the brand gutter (text boxes keep their own measure).
            left_pad = max(0, round(min(min((visible_box(e).x - band.box.x for e in elements), default=0), gutter)))
            right_pad = max(0, round(min(min((band.box.x + band.box.w - visible_box(e).x - visible_box(e).w
                                              for e in elements), default=0), gutter)))
        left, right = band.box.x + left_pad, band.box.x + band.box.w - right_pad
        if right <= left:
            self.decide(band.node_id, "section-width-fallback",
                        "Non-positive section content width; fell back to band edges.", "low")
            left_pad = right_pad = 0
            left, right = band.box.x, band.box.x + band.box.w
        groups: list[list[Row]] = []
        for row in band.rows:
            last = groups[-1][-1] if groups and groups[-1] else None
            if row.card is not None:
                if last is not None and last.card is not None and last.card.node_id == row.card.node_id:
                    groups[-1].append(row)
                else:
                    groups.append([row])
            elif band.background_image or (last is not None and last.card is None and len(row.cells) == 1
                                           and len(last.cells) == 1):
                if not groups:
                    groups.append([])
                groups[-1].append(row)
            else:
                groups.append([row])
        if not groups:
            groups = [[]]
        sections = []
        previous_bottom = band.box.y
        bg = band.background_color or page_bg
        for index, group in enumerate(groups):
            card = group[0].card if group else None
            group_left, group_right, group_left_pad, group_right_pad = left, right, left_pad, right_pad
            if card is not None:
                if not any(band.padding):
                    group_left_pad = max(0, min(round(card.box.x - band.box.x), round(gutter)))
                    group_right_pad = max(0, min(round(band.box.x + band.box.w - card.box.x - card.box.w),
                                                 round(gutter)))
                    group_left, group_right = (band.box.x + group_left_pad,
                                               band.box.x + band.box.w - group_right_pad)
                self.decide(card.node_id, "card-style-dropped",
                            f"Card section keeps the band color; dropped fill {card.fill or 'none'} and stroke "
                            f"{describe_stroke(card.stroke)}.",
                            "medium")
            content_top = card.box.y if card is not None else group[0].box.y if group else 0
            top = self.gap(content_top - previous_bottom, band.node_id, "Section top gap") if group else round(band.box.h)
            content_bottom = card.box.y + card.box.h if card is not None else _bottom(group[-1].box) if group else 0
            bottom = self.gap(band.box.y + band.box.h - content_bottom, band.node_id,
                              "Section bottom gap") if group and index == len(groups) - 1 else 0
            multi = next((r for r in group if len(r.cells) > 1), None)
            align, direction = self.row_options(multi) if multi else (None, None)
            if card is not None and multi:
                align = "middle"
            rows: list[dict] = []
            preceding: Row | None = None
            for row in group:
                count = len(row.cells)
                widths = dnd.split_widths_by_box([(c.box.x, c.box.y, c.box.w, c.box.h) for c in row.cells],
                                                 group_left, group_right)
                if row.card is not None:
                    # Images shrink to their column; only text needs protection from squeezing.
                    cap = 0.6 * (group_right - group_left)
                    minimums = [min(cap, max((e.box.w for e in c.elements if isinstance(e, Text)), default=0))
                                for c in row.cells]
                else:
                    minimums = [max((visible_box(e).w for e in c.elements if isinstance(e, Image)), default=0)
                                for c in row.cells]
                layout = dnd.nearest_column_layout(widths, minimums)
                shares = dnd.COLUMN_PRESETS[count][layout]
                edges = [group_left]
                for share in shares:
                    edges.append(edges[-1] + (group_right - group_left) * share / sum(shares))
                edges[-1] = group_right
                if preceding and card is not None:
                    row_gap = CARD_LABEL_GAP
                else:
                    row_gap = self.gap(row.box.y - _bottom(preceding.box), band.node_id,
                                       "Within-section row gap") if preceding else 0
                if count > 1 and align == "middle" and row_gap:
                    # Row styles have no padding. Keep the first centered blocks at
                    # zero and give the inter-row gap to the preceding row instead.
                    for column in rows[-1]["columns"]:
                        column["blocks"][-1]["data"]["styles"]["block_padding_bottom"] = row_gap
                    row_gap = 0
                columns = []
                for cell, (edge_left, edge_right) in zip(row.cells, pairwise(edges)):
                    blocks = []
                    previous: Element | None = None
                    for element in cell.elements:
                        box = visible_box(element)
                        if previous:
                            pad_top = self.gap(box.y - _bottom(visible_box(previous)), element.node_id,
                                               f"Gap after {previous.node_id}")
                        else:
                            pad_top = row_gap + (0 if count > 1 and align == "middle" else self.gap(
                                box.y - row.box.y, element.node_id, "Gap from row top"))
                        blocks.append(self.block(element, edge_left, edge_right - edge_left, bg, pad_top,
                                                 single_cell=count == 1))
                        previous = element
                    columns.append(blocks)
                rows.append(dnd.row(layout, *columns))
                preceding = row
            section = dnd.section(rows, bg=bg, pad=(top, group_right_pad, bottom, group_left_pad),
                                  column_align=align, column_direction=direction)
            if band.background_image:
                asset = self.asset(band.background_image.node_id)
                section["data"]["styles"].update(background_url=asset["url"], background_asset_id=str(asset["id"]),
                                                   background_format="cover", background_position="center-center",
                                                   background_repeat=False)
            sections.append(_own_content_color(section))
            if group:
                previous_bottom = content_bottom
        return sections

    def page_background(self) -> str:
        """Plain-band color with the greatest total height; else the last band's color."""
        plain = [b for b in self.ir.bands if not b.background_image]
        heights: dict[str, float] = {}
        for band in plain:
            if band.background_color:
                heights[band.background_color] = heights.get(band.background_color, 0) + band.box.h
        if heights:
            color = max(heights, key=lambda color: heights[color])
            self.decide(self.ir.frame_id, "page-background",
                        f"{color} covers {heights[color]:g} px of plain bands.", "medium")
            return color
        last = self.ir.bands[-1].background_color if self.ir.bands else None
        color = last or "#FFFFFF"
        reason = "No photo-free band has a background color" if plain else "Every band has a photo"
        self.decide(self.ir.frame_id, "page-background",
                    f"{reason}; used the last band's color {color}.", "medium")
        return color

    def run(self, name: str | None) -> Conversion:
        page_bg = self.brand.get("page_background") or self.page_background()
        bands = self.prepare()
        self.build_roles(bands)
        sections: list[dict] = []
        previous_bottom = 0.0
        band_gaps = {}
        for band in self.ir.bands:
            band_gaps[band.node_id] = band.box.y - previous_bottom
            previous_bottom = _bottom(band.box)
        for band in bands:
            band_sections = self.sections(band, page_bg)
            band_sections[0]["data"]["styles"]["inner_padding_top"] += self.gap(
                band_gaps[band.node_id], band.node_id,
                "Leading frame gap" if band.node_id == self.ir.bands[0].node_id else "Inter-band gap")
            sections.extend(band_sections)
        if sections:
            sections[-1]["data"]["styles"]["inner_padding_bottom"] += self.gap(
                self.ir.height - previous_bottom, self.ir.bands[-1].node_id, "Trailing frame gap")
        footer = self.brand.get("footer", {})
        if footer.get("mode", "standard") == "none":
            self.decide(self.ir.frame_id, "footer-none", "Brand universal footer must be inserted manually in Klaviyo.")
        else:
            rgb = page_bg.lstrip("#")
            if len(rgb) == 3:
                rgb = "".join(c * 2 for c in rgb)
            channels = [int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            luminance = sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
            color = footer.get("color") or ("#CCCCCC" if luminance < 0.5 else "#555555")
            sections.append(_own_content_color(
                dnd.footer(self.roles["text"].values["font_family"], page_bg, color=color)))
        styles = dnd.template_styles(background=page_bg, text=self.roles["text"].values,
                                      headings={int(role[1]): value.values for role, value in self.roles.items()
                                                if role.startswith("h")},
                                      link={"color": self.brand.get("link_color", "#000000"),
                                            "text_decoration": "underline"}, margin_top=0)
        body = dnd.template_body(self.ir.name if name is None else name, sections, styles, page_bg, round(self.ir.width))
        return Conversion(body, self.decisions, find_todos(body))


def prepare_bands(ir: IR, overrides: dict | None = None) -> list[Band]:
    """The bands emit actually renders: skips, off-canvas images, vertical dividers, card label
    hoisting, row merging, and the four-column limit applied. No input is mutated."""
    return _Emitter(ir, {}, overrides or {}, {}).prepare()


def emit(ir: IR, *, brand: dict | None = None, overrides: dict | None = None,
         assets: dict | None = None, name: str | None = None) -> Conversion:
    """Build a template POST body without exports, network access, or filesystem writes."""
    return _Emitter(ir, brand or {}, overrides or {}, assets or {}).run(name)
