"""Pure Figma node tree → editable email IR. No exports, API calls, or Klaviyo JSON.

All boxes use frame-relative coordinates, preserving fractional geometry. Image
covered_node_ids are visible descendants subsumed by a safe, text-free export.
Layer names are hints, never generated alt text. Decisions flag lossy mappings.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from itertools import combinations, groupby
from math import degrees, isclose
from typing import Literal

from dnd import group_rows_by_box

FRAME_LIKE = {"FRAME", "INSTANCE", "COMPONENT"}
Padding = tuple[float, float, float, float]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class TextStyle:
    family: str | None
    postscript_name: str | None
    size: float | None
    lineHeightPx: float | None
    weight: float | None
    italic: bool
    letterSpacing: float
    align: str
    color: str | None


@dataclass(frozen=True)
class Segment:
    text: str
    style: TextStyle


@dataclass(frozen=True)
class Text:
    node_id: str
    box: Box
    characters: str
    style: TextStyle
    segments: list[Segment]
    rotation_deg: float = 0
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class Image:
    node_id: str
    box: Box
    export: Literal["node", "fill"]
    name_hint: str
    image_ref: str | None = None
    masked: bool = False
    clipped_box: Box | None = None
    covered_node_ids: list[str] = field(default_factory=list)
    paints: list[dict] = field(default_factory=list)
    vector: bool = False
    kind: Literal["image"] = field(default="image", init=False)


@dataclass(frozen=True)
class Stroke:
    color: str
    weight: float


@dataclass(frozen=True)
class Button:
    node_id: str
    box: Box
    label: Text
    fill: str | None
    padding: Padding
    corner_radius: float
    stroke: Stroke | None
    kind: Literal["button"] = field(default="button", init=False)


@dataclass(frozen=True)
class Divider:
    node_id: str
    box: Box
    color: str | None
    weight: float
    orientation: Literal["horizontal", "vertical"]
    kind: Literal["divider"] = field(default="divider", init=False)


@dataclass(frozen=True)
class Unknown:
    node_id: str
    box: Box
    node_type: str
    reason: str
    kind: Literal["unknown"] = field(default="unknown", init=False)


Element = Text | Image | Button | Divider | Unknown


@dataclass(frozen=True)
class Cell:
    box: Box
    elements: list[Element]


@dataclass(frozen=True)
class Card:
    """A full-width outlined/filled panel whose content is regrouped into one row."""
    node_id: str
    box: Box
    fill: str | None
    stroke: Stroke | None


@dataclass(frozen=True)
class Row:
    box: Box
    cells: list[Cell]
    card: Card | None = None


@dataclass(frozen=True)
class Band:
    node_id: str
    name: str
    box: Box
    background_color: str | None
    background_image: Image | None
    padding: Padding
    rows: list[Row]


@dataclass(frozen=True)
class Decision:
    node_id: str
    rule: str
    detail: str
    confidence: Confidence


@dataclass(frozen=True)
class IR:
    frame_id: str
    name: str
    width: float
    height: float
    bands: list[Band]
    decisions: list[Decision]

    def to_dict(self) -> dict:
        return asdict(self)


def _walk(node: dict) -> Iterator[dict]:
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def is_hidden(node: dict) -> bool:
    """True for nodes the converter prunes: visible=false or opacity=0."""
    return node.get("visible", True) is False or node.get("opacity", 1) == 0


def _visible_tree(node: dict, decisions: list[Decision]) -> dict | None:
    if is_hidden(node):
        decisions.append(Decision(node["id"], "skipped-hidden",
                                  "Skipped subtree: visible=false or opacity=0.", "high"))
        return None
    result = dict(node)
    result["children"] = [visible for child in node.get("children") or []
                          if (visible := _visible_tree(child, decisions)) is not None]
    return result


def _paints(node: dict, kind: str = "fills") -> Iterator[dict]:
    return (paint for paint in node.get(kind) or []
            if paint.get("visible", True) and paint.get("opacity", 1) != 0)


def _color(node: dict, kind: str = "fills") -> str | None:
    # Figma paints bottom to top, so the visible color is the LAST visible solid.
    for paint in reversed(list(_paints(node, kind))):
        if paint.get("type") == "SOLID":
            rgb = [round(paint["color"][key] * 255) for key in "rgb"]
            return "#{:02X}{:02X}{:02X}".format(*rgb)
    return None


topmost_color = _color  # public name for the converter's solid-color rule


def _image_fill(node: dict) -> dict | None:
    return next((paint for paint in _paints(node) if paint.get("type") == "IMAGE"), None)


def _padding(node: dict) -> Padding:
    if node.get("layoutMode") not in {"HORIZONTAL", "VERTICAL"}:
        return (0, 0, 0, 0)
    return (float(node.get("paddingTop", 0)), float(node.get("paddingRight", 0)),
            float(node.get("paddingBottom", 0)), float(node.get("paddingLeft", 0)))


def _box(node: dict, origin: dict) -> Box:
    box = node.get("absoluteBoundingBox") or {}
    return Box(float(box.get("x", origin.get("x", 0))) - origin.get("x", 0),
               float(box.get("y", origin.get("y", 0))) - origin.get("y", 0),
               float(box.get("width", 0)), float(box.get("height", 0)))


def _intersection(a: Box, b: Box) -> Box:
    x, y = max(a.x, b.x), max(a.y, b.y)
    return Box(x, y, max(0, min(a.x + a.w, b.x + b.w) - x),
               max(0, min(a.y + a.h, b.y + b.h) - y))


def _overlaps(a: Box, b: Box) -> bool:
    overlap = _intersection(a, b)
    return overlap.w > 0 and overlap.h > 0


def envelope(boxes: list[Box]) -> Box:
    x, y = min(box.x for box in boxes), min(box.y for box in boxes)
    return Box(x, y, max(box.x + box.w for box in boxes) - x,
               max(box.y + box.h for box in boxes) - y)


def _style(node: dict, style: dict) -> TextStyle:
    return TextStyle(style.get("fontFamily"), style.get("fontPostScriptName"),
                     style.get("fontSize"), style.get("lineHeightPx"), style.get("fontWeight"),
                     bool(style.get("italic", "italic" in style.get("fontStyle", "").lower())),
                     style.get("letterSpacing", 0),
                     style.get("textAlignHorizontal", "LEFT").lower(),
                     _color(style) if "fills" in style else _color(node))


def _case(text: str, style: dict) -> str:
    case = style.get("textCase")
    return text.upper() if case == "UPPER" else text.lower() if case == "LOWER" else text


def _text(node: dict, origin: dict, decisions: list[Decision]) -> Text:
    style = node.get("style") or {}
    chars = node.get("characters", "")
    overrides = node.get("characterStyleOverrides") or []
    table = node.get("styleOverrideTable") or {}
    segments: list[Segment] = []
    if overrides:
        # Figma indexes UTF-16 code units; Python indexes Unicode code points.
        offset = 0
        indexed: list[tuple[int, str]] = []
        for char in chars:
            override = overrides[offset] if offset < len(overrides) else 0
            indexed.append((override, char))
            offset += 2 if ord(char) > 0xFFFF else 1
        for override, run in groupby(indexed, key=lambda item: item[0]):
            merged = {**style, **table.get(str(override), {})} if override else style
            segments.append(Segment(_case("".join(char for _, char in run), merged),
                                    _style(node, merged)))
        decisions.append(Decision(node["id"], "mixed-text-styles",
                                  f"Preserved {len(segments)} character-style runs.", "high"))
    angle = degrees(node.get("rotation", 0))
    if round(angle) != 0 and isclose(angle, round(angle), abs_tol=0.00001):
        angle = float(round(angle))
    if node.get("rotation", 0) != 0:
        decisions.append(Decision(node["id"], "rotated-text",
                                  f"Preserved {angle:g} degrees as live text; rendering needs review.", "low"))
    return Text(node["id"], _box(node, origin), _case(chars, style),
                _style(node, style), segments, angle)


def _fill_paints(node: dict) -> list[dict]:
    paints: list[dict] = []
    for paint in _paints(node):
        result = {key: paint[key] for key in ("type", "blendMode") if key in paint}
        result["opacity"] = paint.get("opacity", 1)
        if paint.get("type") == "IMAGE":
            result.update({key: paint[key] for key in (
                "imageRef", "scaleMode", "imageTransform", "scalingFactor", "rotation", "filters")
                if key in paint})
        elif paint.get("type") == "SOLID" and "color" in paint:
            result["color"] = _color({"fills": [paint]})
        elif paint.get("type", "").startswith("GRADIENT_"):
            if "gradientStops" in paint:
                result["gradientStops"] = [
                    {"color": "#{:02X}{:02X}{:02X}".format(
                        *(round(stop["color"][key] * 255) for key in "rgb")),
                     "alpha": stop["color"].get("a", 1), "position": stop["position"]}
                    for stop in paint["gradientStops"]]
            if "gradientHandlePositions" in paint:
                result["gradientHandlePositions"] = paint["gradientHandlePositions"]
        paints.append(result)
    return paints


def _image(node: dict, origin: dict, band_box: Box, decisions: list[Decision], *,
           export: Literal["node", "fill"] = "node",
           masked: bool = False, vector: bool = False) -> Image:
    box = _box(node, origin)
    clipped = _intersection(box, band_box)
    paint = _image_fill(node)
    paints = _fill_paints(node) if export == "fill" else []
    if paints and not (len(paints) == 1 and paints[0]["type"] == "IMAGE"
                       and paints[0].get("imageTransform", [[1, 0, 0], [0, 1, 0]]) == [[1, 0, 0], [0, 1, 0]]
                       and paints[0].get("scaleMode", "FILL") == "FILL"):
        details: list[str] = []
        for fill in paints:
            if fill["type"] == "IMAGE":
                if fill.get("imageTransform", [[1, 0, 0], [0, 1, 0]]) != [[1, 0, 0], [0, 1, 0]]:
                    details.append("crop transform")
                if fill.get("scaleMode", "FILL") != "FILL":
                    details.append(f"{fill['scaleMode']} scale mode")
            else:
                overlay = " overlay" if fill["type"] == "SOLID" else ""
                details.append(f"{fill['type']}{overlay} {fill['opacity']:.0%}")
        decisions.append(Decision(node["id"], "composite-fill",
                                  "Composite ordered fill stack: " + "; ".join(
                                      details or [f"{len(paints)} IMAGE fills"]) + ".", "medium"))
    return Image(node["id"], box, export, node.get("name", ""),
                 paint.get("imageRef") if export == "fill" and paint else None,
                 masked, clipped if clipped != box else None,
                 [child["id"] for child in list(_walk(node))[1:]] if export == "node" else [], paints,
                 vector)


def _vectors_only(node: dict) -> bool:
    if node.get("type") in {"VECTOR", "BOOLEAN_OPERATION"}:
        return not any(child.get("type") == "TEXT" for child in _walk(node))
    children = node.get("children") or []
    return (node.get("type") in {"GROUP", "FRAME"} and bool(children)
            and all(_vectors_only(child) for child in children))


def _elements(node: dict, origin: dict, band_box: Box, decisions: list[Decision],
              shapes: list[dict], *, is_band: bool = False,
              frames: list[dict] | None = None) -> list[Element]:
    node_type = node.get("type", "UNKNOWN")
    box = _box(node, origin)
    descendants = list(_walk(node))
    texts = [child for child in descendants if child.get("type") == "TEXT"]
    if node_type == "TEXT":
        return [_text(node, origin, decisions)]
    if not is_band and node_type in {"FRAME", "INSTANCE"} and len(texts) == 1 and box.h <= 80 and (
            _color(node) or _color(node, "strokes")) and not any(
                _image_fill(child) for child in descendants):
        color = _color(node, "strokes")
        decisions.append(Decision(node["id"], "button",
                                  "One visible text descendant, solid fill/stroke, height <=80, no images.",
                                  "high"))
        dropped = [f"{child['id']}:{child.get('type', 'UNKNOWN')}" for child in descendants[1:]
                   if child is not texts[0] and (child.get("type") not in {"FRAME", "GROUP", "INSTANCE"}
                                                or any(_paints(child)) or any(_paints(child, "strokes")))]
        if dropped:
            decisions.append(Decision(node["id"], "button-artwork-dropped",
                                      f"Button keeps only its live label; dropped {', '.join(dropped)}.", "low"))
        return [Button(node["id"], box, _text(texts[0], origin, decisions), _color(node),
                       _padding(node), node.get("cornerRadius", 0),
                       Stroke(color, node.get("strokeWeight", 1)) if color else None)]
    masked = node_type == "GROUP" and any(
        child.get("isMask") for child in node.get("children") or [])
    if masked:
        decisions.append(Decision(node["id"], "mask-group",
                                  "Text-free mask group exported once." if not texts else
                                  "Mask contains text: flattened conservatively to keep words live.",
                                  "high" if not texts else "low"))
        if not texts:
            return [_image(node, origin, band_box, decisions, masked=True)]
    if _image_fill(node) is not None and not texts:
        return [_image(node, origin, band_box, decisions)]
    if _vectors_only(node):
        decisions.append(Decision(node["id"], "vector-image",
                                  "Text-free vector artwork exported as one image.", "high"))
        return [_image(node, origin, band_box, decisions, vector=True)]
    color = _color(node) or _color(node, "strokes")
    if node_type == "LINE" or (node_type == "RECTANGLE" and not node.get("children")
                                and min(box.w, box.h) <= 2 and color):
        vertical = box.h > box.w
        if node_type == "LINE" and isclose(abs(degrees(node.get("rotation", 0))), 90,
                                            abs_tol=0.00001):
            vertical = True
        if vertical:
            decisions.append(Decision(node["id"], "vertical-divider",
                                      "Vertical rule preserved; email cannot render vertical rules.", "low"))
        return [Divider(node["id"], box, _color(node, "strokes") or _color(node),
                        node.get("strokeWeight", min(box.w, box.h) or 1),
                        "vertical" if vertical else "horizontal")]
    if node_type in {"RECTANGLE", "ELLIPSE"} and not node.get("children") and color:
        shapes.append(node)
        return []
    elements: list[Element] = []
    if _image_fill(node) is not None:
        elements.append(_image(node, origin, band_box, decisions, export="fill"))
        decisions.append(Decision(node["id"], "image-fill",
                                  "Used raw fill because node export would bake in live text.", "high"))
    if (frames is not None and not is_band and node_type in {"FRAME", "INSTANCE"}
            and _image_fill(node) is None and (_color(node) or _color(node, "strokes"))):
        frames.append(node)
    if node_type in {"FRAME", "GROUP", "INSTANCE", "COMPONENT", "COMPONENT_SET"}:
        decisions.append(Decision(node["id"], "flatten-container",
                                  "Flattened container into live leaf elements.", "high"))
    else:
        reason = "Unsupported node type or unpainted shape; retained for review."
        elements.append(Unknown(node["id"], box, node_type, reason))
        decisions.append(Decision(node["id"], "unknown", reason, "low"))
    for child in node.get("children") or []:
        elements.extend(_elements(child, origin, band_box, decisions, shapes, frames=frames))
    return elements


def _background(elements: list[Element], band_box: Box, node: dict, origin: dict,
                order: dict[str, int], decisions: list[Decision]) -> tuple[list[Element], Image | None]:
    candidates: list[tuple[Image, float, list[str], list[str]]] = []
    area = band_box.w * band_box.h
    for element in elements:
        if not isinstance(element, Image) or element.node_id == node["id"] or area <= 0:
            continue
        clipped = _intersection(element.box, band_box)
        coverage = clipped.w * clipped.h / area
        overlapping = [other for other in elements if other is not element
                       and _overlaps(element.box, other.box)]
        if coverage >= 0.6 and any(isinstance(other, (Text, Button, Image)) for other in overlapping):
            earlier = [other.node_id for other in overlapping if order[other.node_id] < order[element.node_id]]
            candidates.append((element, coverage, [other.node_id for other in overlapping], earlier))
    valid = [candidate for candidate in candidates if not candidate[3]]
    full_band = [candidate for candidate in valid if candidate[1] >= 0.99]
    own_fill = node.get("type") in FRAME_LIKE and _image_fill(node) is not None
    chosen = (max(full_band, key=lambda candidate: order[candidate[0].node_id]) if full_band else
              valid[0] if len(valid) == 1 and not own_fill else None)
    background: Image | None = None
    if chosen is not None:
        image, coverage, overlaps, _ = chosen
        background = image
        decisions.append(Decision(image.node_id, "band-background-image",
                                  f"Covers {coverage:.1%} of band; overlaps {', '.join(overlaps)}; "
                                  f"all paint after image at DFS index {order[image.node_id]}.", "high"))
        elements = [element for element in elements if element is not image]
        if own_fill:
            decisions.append(Decision(node["id"], "occluded-fill",
                                      f"Band IMAGE fill paints first; {image.node_id} at DFS index "
                                      f"{order[image.node_id]} covers {coverage:.1%} of band and paints above it; "
                                      "omitted the frame fill.", "high"))
    elif own_fill:
        background = _image(node, origin, band_box, decisions, export="fill")
        decisions.append(Decision(node["id"], "band-background-image",
                                  "Band frame has an IMAGE fill; raw fill keeps all descendants live.", "high"))
    for image, coverage, overlaps, earlier in candidates:
        if background is image:
            continue
        reason = (f"covers earlier-painted elements: {', '.join(earlier)}" if earlier else
                  "band already has a background or multiple valid candidates exist")
        decisions.append(Decision(image.node_id, "background-image-ambiguous",
                                  f"Kept as content: covers {coverage:.1%}, overlaps {', '.join(overlaps)}; "
                                  f"{reason}.", "low"))
    return elements, background


def _x_groups(elements: list[Element]) -> list[list[Element]]:
    """Group elements in the given order; a new group starts when x clears the running right edge."""
    groups: list[list[Element]] = []
    right = float("-inf")
    for element in elements:
        if not groups or element.box.x >= right:
            groups.append([element])
            right = element.box.x + element.box.w
        else:
            groups[-1].append(element)
            right = max(right, element.box.x + element.box.w)
    return groups


def _note_overlaps(members: list[Element], decisions: list[Decision]) -> None:
    for a, b in combinations(members, 2):
        if _overlaps(a.box, b.box):
            decisions.append(Decision(a.node_id, "overlap",
                                      f"Overlaps {b.node_id} in both axes; kept in one reading-order cell.",
                                      "low"))


def _rows(elements: list[Element], decisions: list[Decision]) -> list[Row]:
    rows: list[Row] = []
    for members in group_rows_by_box(elements, lambda e: (e.box.x, e.box.y, e.box.w, e.box.h)):
        groups = _x_groups(members)
        _note_overlaps(members, decisions)
        cells = [Cell(envelope([e.box for e in group]),
                      sorted(group, key=lambda e: (e.box.y, e.box.x, e.node_id))) for group in groups]
        if len(cells) > 1:
            decisions.append(Decision(members[0].node_id, "columns",
                                      f"Geometry yields {len(cells)} left-to-right cells.", "medium"))
        rows.append(Row(envelope([element.box for element in members]), cells))
    return rows


def is_quarter_turn(element: Element) -> bool:
    return isinstance(element, Text) and any(abs(abs(element.rotation_deg) - angle) <= 1
                                             for angle in (90, 270))


def visible_box(element: Element) -> Box:
    return element.clipped_box if isinstance(element, Image) and element.clipped_box else element.box


def _area(box: Box) -> float:
    return box.w * box.h


def describe_stroke(stroke: Stroke | None) -> str:
    return f"{stroke.color} {stroke.weight:g} px" if stroke else "none"


def _is_backdrop(box: Box, band_box: Box) -> bool:
    """A shape or frame spanning the band (>= 95% wide, >= 90% tall) is the band's background, not a card."""
    return box.w >= 0.95 * band_box.w and box.h >= 0.9 * band_box.h


def _card_cells(members: list[Element], decisions: list[Decision]) -> list[Cell]:
    """Cells by media side: images left of the text, the text cells (x-gap rule), images right of it."""
    live = [e for e in members if isinstance(e, (Text, Button)) and not is_quarter_turn(e)]
    images = [e for e in members if isinstance(e, Image)]
    rest = [e for e in members if e not in live and e not in images]
    ordered: list[list[Element]]
    if live:
        live_box = envelope([e.box for e in live])
        center = live_box.x + live_box.w / 2
        left = [e for e in images if visible_box(e).x + visible_box(e).w / 2 < center]
        right = [e for e in images if e not in left]
        groups = _x_groups(sorted(live, key=lambda e: (e.box.x, e.box.y, e.node_id)))
        groups[0].extend(rest)
        ordered = [[*left], *groups, [*right]]
    else:
        ordered = [[*images, *rest]]
    cells = [Cell(envelope([e.box for e in group]), sorted(group, key=lambda e: (e.box.y, e.box.x, e.node_id)))
             for group in ordered if group]
    if len(cells) > 1:
        decisions.append(Decision(members[0].node_id, "columns",
                                  f"Card media side yields {len(cells)} left-to-right cells.", "medium"))
    return cells


def _regroup_cards(elements: list[Element], band_box: Box, band_color: str | None, origin: dict,
                   order: dict[str, int], shapes: list[dict], frames: list[dict],
                   decisions: list[Decision], *, regroup: bool) -> tuple[list[Element], list[Row]]:
    """Regroup content backed by full-width cards into one row per card; flag narrow cards."""
    cards: list[tuple[Card, set[str]]] = []
    for shape in shapes:
        cards.append((_card(shape, origin), set()))
    for frame in frames:
        fill = _color(frame)
        if (fill and fill != band_color) or _color(frame, "strokes"):
            cards.append((_card(frame, origin), {n["id"] for n in _walk(frame)}))
    cards = [entry for entry in cards if not _is_backdrop(entry[0].box, band_box)]
    cards.sort(key=lambda entry: order[entry[0].node_id])

    def eligible(card: Card, descendants: set[str], element: Element) -> bool:
        return element.node_id in descendants or order[card.node_id] < order[element.node_id]

    wide = [entry for entry in cards if entry[0].box.w >= 0.6 * band_box.w]
    assigned: dict[str, list[Element]] = {entry[0].node_id: [] for entry in wide}
    if regroup:
        for element in elements:
            scored = [(_area(_intersection(card.box, visible_box(element))), -_area(card.box), card.node_id)
                      for card, descendants in wide if eligible(card, descendants, element)]
            scored = [entry for entry in scored if entry[0] > 0]
            if scored:
                assigned[max(scored)[2]].append(element)
    rows: list[Row] = []
    grouped: set[str] = set()
    for card, _ in wide:
        members = assigned[card.node_id]
        if not members:
            continue
        grouped.update(element.node_id for element in members)
        stroke = f", stroke {describe_stroke(card.stroke)}" if card.stroke else ""
        decisions.append(Decision(card.node_id, "card-group",
                                  f"Full-width card ({card.box.w:g} px, fill {card.fill or 'none'}{stroke}) "
                                  f"groups {', '.join(e.node_id for e in members)} into one row.", "medium"))
        _note_overlaps(members, decisions)
        rows.append(Row(card.box, _card_cells(members, decisions), card))
    for card, descendants in cards:
        if assigned.get(card.node_id):
            continue
        backs = any(eligible(card, descendants, e) and _overlaps(card.box, visible_box(e)) for e in elements)
        if not backs:
            continue
        stroke = f", stroke {describe_stroke(card.stroke)}" if card.stroke else ""
        decisions.append(Decision(card.node_id, "card-frame",
                                  f"Card frame ({card.box.w:g} px wide, fill {card.fill or 'none'}{stroke}) "
                                  "is not reproduced; content keeps its normal rows.", "low"))
    return [e for e in elements if e.node_id not in grouped], rows


def _card(node: dict, origin: dict) -> Card:
    color = _color(node, "strokes")
    return Card(node["id"], _box(node, origin), _color(node),
                Stroke(color, node.get("strokeWeight", 1)) if color else None)


def build_ir(tree: dict, frame_id: str | None = None) -> IR:
    """Build an IR from a Figma nodes response (or a document/root node).

    Without frame_id, choose the lexically first nodes-response key. Hidden
    subtrees are pruned before classification, including hidden button labels.
    The input is never modified. Unsupported leaves remain explicit unknowns.
    """
    if "nodes" in tree:
        nodes = tree["nodes"]
        selected = frame_id if frame_id is not None else min(nodes)
        root = nodes[selected]["document"]
    else:
        root = tree.get("document", tree)
        if frame_id is not None:
            root = next(node for node in _walk(root) if node.get("id") == frame_id)
    origin = root.get("absoluteBoundingBox") or {}
    decisions: list[Decision] = []
    visible = _visible_tree(root, decisions)
    bands: list[Band] = []
    for node in sorted(visible.get("children", []) if visible else [],
                       key=lambda n: (_box(n, origin).y, _box(n, origin).x, n["id"])):
        box = _box(node, origin)
        shapes: list[dict] = []
        frames: list[dict] = []
        elements: list[Element] = []
        if node.get("type") in FRAME_LIKE and _image_fill(node) is not None:
            for child in node.get("children") or []:
                elements.extend(_elements(child, origin, box, decisions, shapes, frames=frames))
        else:
            elements = _elements(node, origin, box, decisions, shapes, is_band=True, frames=frames)
        # Source order is paint order: a card must precede content it backs.
        order = {child["id"]: index for index, child in enumerate(_walk(node))}
        card_shapes: list[dict] = []
        for shape in shapes:
            backed = [e.node_id for e in elements if order[shape["id"]] < order[e.node_id]
                      and _overlaps(_box(shape, origin), e.box)]
            if backed:
                card_shapes.append(shape)
            decisions.append(Decision(shape["id"], "card-background" if backed else "solid-shape",
                                      f"Omitted solid shape {_color(shape) or _color(shape, 'strokes')}; "
                                      f"behind {', '.join(backed)}." if backed else
                                      "Omitted decorative solid shape without live content.", "high"))
        elements, background = _background(elements, box, node, origin, order, decisions)
        elements, card_rows = _regroup_cards(elements, box, _color(node), origin, order, card_shapes, frames,
                                             decisions, regroup=background is None)
        rows = _rows(elements, decisions)
        if card_rows:
            indexed = sorted(enumerate([*rows, *card_rows]), key=lambda item: (item[1].box.y, item[0]))
            rows = [row for _, row in indexed]
        bands.append(Band(node["id"], node.get("name", ""), box, _color(node), background,
                          _padding(node), rows))
    return IR(root["id"], root.get("name", ""), origin.get("width", 0), origin.get("height", 0),
              bands, decisions)
