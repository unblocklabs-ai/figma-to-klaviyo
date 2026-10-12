import json
from collections import Counter

import dnd
import ir
import pytest


def all_elements(result):
    return [element for band in result.bands for row in band.rows
            for cell in row.cells for element in cell.elements]


def node(node_id, node_type, box, **properties):
    x, y, width, height = box
    return {"id": node_id, "name": node_id, "type": node_type,
            "absoluteBoundingBox": {"x": x, "y": y, "width": width, "height": height},
            **properties}


def document(*children):
    return node("frame", "FRAME", (100, 200, 100, 100), children=[
        node("band", "FRAME", (100, 200, 100, 100), children=list(children))])


def test_visibility_prunes_hidden_ancestors_opacity_and_consumed_graphics():
    root = document(
        node("hidden", "FRAME", (100, 200, 100, 100), visible=False, children=[
            node("nested-hidden", "TEXT", (100, 200, 10, 10), visible=False, characters="no")]),
        node("transparent", "GROUP", (100, 200, 10, 10), opacity=0, children=[
            node("transparent-text", "TEXT", (100, 200, 10, 10), characters="no")]),
        node("logo", "GROUP", (100, 200, 10, 10), children=[
            node("vector", "VECTOR", (100, 200, 10, 10)),
            node("old-logo", "VECTOR", (100, 200, 10, 10), visible=False)]),
        node("live", "TEXT", (100, 230, 10, 10), characters="yes"))
    result = ir.build_ir(root)
    assert [element.node_id for element in all_elements(result)] == ["logo", "live"]
    assert all_elements(result)[0].covered_node_ids == ["vector"]
    assert Counter(d.node_id for d in result.decisions if d.rule == "skipped-hidden") == {
        "hidden": 1, "transparent": 1, "old-logo": 1}
    hidden_frame = ir.build_ir({**root, "opacity": 0})
    assert hidden_frame.bands == []
    assert [(d.node_id, d.rule) for d in hidden_frame.decisions] == [("frame", "skipped-hidden")]


def test_text_style_overrides_inherit_defaults_and_use_utf16_indices():
    root = document(node("text", "TEXT", (101.25, 202.5, 50, 20), characters="a😀ßZ!",
                         style={"fontFamily": "Base", "fontPostScriptName": "Base-Regular",
                                "fontSize": 20, "fontWeight": 400, "lineHeightPx": 28,
                                "textCase": "UPPER", "letterSpacing": 0.5,
                                "textAlignHorizontal": "RIGHT"},
                         fills=[{"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0}}],
                         characterStyleOverrides=[0, 1, 1, 2, 0],
                         styleOverrideTable={"1": {"italic": True},
                                             "2": {"fontSize": 17, "fills": [
                                                 {"type": "SOLID", "color": {"r": 0, "g": 0, "b": 1}}]}}))
    text = all_elements(ir.build_ir(root))[0]
    assert text.characters == "A😀SSZ!"
    assert [segment.text for segment in text.segments] == ["A", "😀", "SS", "Z!"]
    assert text.style == ir.TextStyle("Base", "Base-Regular", 20, 28, 400, False, 0.5, "right", "#FF0000")
    assert text.segments[1].style.italic
    assert text.segments[1].style.family == "Base"
    assert text.segments[2].style.color == "#0000FF"
    assert text.box == ir.Box(1.25, 2.5, 50, 20)


def test_lowercase_mixed_text_uses_contextual_word_case():
    root = document(node("text", "TEXT", (100, 200, 20, 10), characters="ΟΣ",
                         style={"textCase": "LOWER"}, characterStyleOverrides=[0, 0]))
    text = all_elements(ir.build_ir(root))[0]
    assert text.characters == "ος"
    assert [segment.text for segment in text.segments] == ["ος"]


def test_image_container_and_unknown_never_swallow_live_text():
    root = document(node("photo-container", "FRAME", (100, 200, 100, 100),
                         fills=[{"type": "IMAGE", "imageRef": "raw-photo"}], children=[
                             node("copy", "TEXT", (120, 220, 50, 20), characters="live"),
                             node("unsupported", "STAR", (100, 250, 20, 20))]))
    result = ir.build_ir(root)
    elements = all_elements(result)
    photo = result.bands[0].background_image
    assert photo.node_id == "photo-container"
    assert photo.export == "fill"
    assert photo.image_ref == "raw-photo"
    assert photo.covered_node_ids == []
    assert photo.paints == [{"type": "IMAGE", "opacity": 1, "imageRef": "raw-photo"}]
    assert not any(isinstance(element, ir.Image) for element in elements)
    assert any(d.node_id == photo.node_id and d.rule == "band-background-image"
               and d.confidence == "high" for d in result.decisions)
    assert not any(d.node_id == photo.node_id and d.rule == "composite-fill" for d in result.decisions)
    assert [(text.node_id, text.characters) for text in elements if isinstance(text, ir.Text)] == [
        ("copy", "live")]
    assert any(isinstance(element, ir.Unknown) and element.node_type == "STAR" for element in elements)
    assert any(d.node_id == "unsupported" and d.rule == "unknown" for d in result.decisions)


@pytest.mark.parametrize("photo_first", [False, True], ids=["text-before-photo", "photo-before-text"])
def test_background_promotion_respects_sibling_paint_order(photo_first):
    photo = node("photo", "RECTANGLE", (100, 200, 100, 100),
                 fills=[{"type": "IMAGE", "imageRef": "photo"}])
    text = node("copy", "TEXT", (120, 220, 50, 20), characters="live")
    result = ir.build_ir(document(*(photo, text) if photo_first else (text, photo)))
    background = result.bands[0].background_image
    assert [(e.node_id, e.characters) for e in all_elements(result) if isinstance(e, ir.Text)] == [
        ("copy", "live")]
    if photo_first:
        assert background.node_id == "photo" and background.export == "node"
        assert not any(e.node_id == "photo" for e in all_elements(result))
        assert not any(d.rule == "background-image-ambiguous" for d in result.decisions)
    else:
        assert background is None
        assert any(e.node_id == "photo" for e in all_elements(result))
        decisions = [d for d in result.decisions if d.rule == "background-image-ambiguous"]
        assert len(decisions) == 1 and decisions[0].node_id == "photo"
        assert decisions[0].confidence == "low"
        assert "earlier-painted" in decisions[0].detail and "copy" in decisions[0].detail


@pytest.mark.parametrize("own_fill,widths,expected_background,ambiguous", [
    (True, [80], "band", ["photo-0"]),
    (True, [100, 100], "photo-0", ["photo-1"]),
    (False, [70, 70], None, ["photo-0", "photo-1"]),
    (False, [80, 80], "photo-0", ["photo-1"]),
])
def test_background_candidates_respect_fill_fallback_and_ambiguity(
        own_fill, widths, expected_background, ambiguous):
    photos = [node(f"photo-{index}", "FRAME", (100, 200 + index * 10, width, 100),
                   fills=[{"type": "IMAGE", "imageRef": f"photo-{index}"}], children=[
                       node(f"art-{index}", "VECTOR", (100, 200 + index * 10, 10, 10))])
              for index, width in enumerate(widths)]
    root = document(*photos, node("copy", "TEXT", (120, 240, 50, 20), characters="live"))
    if own_fill:
        root["children"][0]["fills"] = [{"type": "IMAGE", "imageRef": "band-fill"}]
    if widths == [70, 70]:
        # An earlier live element invalidates both otherwise-large candidates.
        root["children"][0]["children"].insert(0, node(
            "earlier-copy", "TEXT", (120, 240, 50, 20), characters="earlier"))
    result = ir.build_ir(root)
    background = result.bands[0].background_image
    assert (background.node_id if background else None) == expected_background
    assert [(d.node_id, d.confidence) for d in result.decisions
            if d.rule == "background-image-ambiguous"] == [(node_id, "low") for node_id in ambiguous]
    assert {e.node_id for e in all_elements(result) if isinstance(e, ir.Image)} == set(ambiguous)
    if expected_background == "photo-0":
        assert background.export == "node" and background.covered_node_ids == ["art-0"]
    assert [d.node_id for d in result.decisions if d.rule == "occluded-fill"] == (
        ["band"] if own_fill and expected_background == "photo-0" else [])


@pytest.mark.parametrize("paint,composite", [
    ({"type": "IMAGE", "imageRef": "raw", "scaleMode": "FILL",
      "imageTransform": [[1, 0, 0], [0, 1, 0]]}, False),
    ({"type": "IMAGE", "imageRef": "raw", "scaleMode": "FIT"}, True),
    ({"type": "IMAGE", "imageRef": "raw", "scaleMode": "STRETCH",
      "imageTransform": [[0.5, 0, 0.25], [0, 1, 0]], "blendMode": "MULTIPLY",
      "opacity": 0.75, "scalingFactor": 2, "rotation": 15, "filters": {"contrast": 0.2}}, True),
])
def test_fill_exports_keep_compositor_keys_and_flag_nontrivial_stacks(paint, composite):
    root = document(node("copy", "TEXT", (120, 220, 50, 20), characters="live"))
    root["children"][0]["fills"] = [
        {**paint, "visible": True, "boundVariables": {"irrelevant": "metadata"}},
        {"type": "SOLID", "visible": False}, {"type": "SOLID", "opacity": 0},
    ]
    result = ir.build_ir(root)
    image = result.bands[0].background_image
    assert image.paints == [{"opacity": 1, **paint}]
    assert [d.confidence for d in result.decisions if d.rule == "composite-fill"] == (
        ["medium"] if composite else [])
    json.dumps(result.to_dict())


def test_button_records_dropped_artwork_but_not_layout_only_descendants():
    solid = {"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}
    root = document(node("button", "INSTANCE", (100, 200, 100, 48), fills=[solid], children=[
        node("layout", "FRAME", (100, 200, 100, 48), fills=[{**solid, "visible": False}], children=[
            node("group", "GROUP", (100, 200, 100, 48), children=[
                node("instance", "INSTANCE", (100, 200, 100, 48), children=[
                    node("label", "TEXT", (110, 214, 30, 20), characters="BUY"),
                    node("arrow", "VECTOR", (150, 214, 20, 20), strokes=[solid]),
                    node("hidden-arrow", "VECTOR", (150, 214, 20, 20), visible=False, fills=[solid]),
                ]),
            ]),
        ]),
    ]))
    result = ir.build_ir(root)
    elements = all_elements(result)
    assert len(elements) == 1 and isinstance(elements[0], ir.Button)
    assert elements[0].label.characters == "BUY"
    decisions = [d for d in result.decisions if d.rule == "button-artwork-dropped"]
    assert len(decisions) == 1 and decisions[0].node_id == "button"
    assert decisions[0].confidence == "low"
    assert "arrow:VECTOR" in decisions[0].detail
    assert all(descendant not in decisions[0].detail for descendant in [
        "layout:FRAME", "group:GROUP", "instance:INSTANCE", "label:TEXT", "hidden-arrow:VECTOR"])


def test_short_top_level_band_remains_text_not_button():
    root = node("frame", "FRAME", (0, 0, 600, 40), children=[
        node("announcement", "FRAME", (0, 0, 600, 40),
             fills=[{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}], children=[
                 node("copy", "TEXT", (20, 10, 560, 20), characters="Announcement")]),
    ])
    result = ir.build_ir(root)
    elements = all_elements(result)
    assert len(result.bands) == 1
    assert len(elements) == 1 and isinstance(elements[0], ir.Text)
    assert elements[0].characters == "Announcement"
    assert not any(d.rule == "button" for d in result.decisions)


def test_shared_row_helper_handles_overlap_threshold_and_transitive_span():
    boxes = [(60, 5, 10, 10), (0, 0, 10, 10), (20, 9, 10, 10),
             (40, 8, 10, 10), (0, 19, 10, 10)]
    assert dnd.group_rows_by_box(boxes, lambda box: box) == [
        [(0, 0, 10, 10), (20, 9, 10, 10), (40, 8, 10, 10), (60, 5, 10, 10)],
        [(0, 19, 10, 10)]]
    assert dnd.group_rows_by_box([(0, 0, 10, 10), (0, 9, 10, 10)], lambda box: box) == [
        [(0, 0, 10, 10)], [(0, 9, 10, 10)]]


def _solid(hex_value, **extra):
    rgb = [int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return {"type": "SOLID", "color": dict(zip("rgb", rgb)), **extra}


def test_color_reads_topmost_visible_solid_for_fills_and_strokes():
    node = {"fills": [_solid("#FFFFFF"), _solid("#000000")],
            "strokes": [_solid("#FF0000"), _solid("#00FF00"), _solid("#0000FF", visible=False)]}
    assert ir._color(node) == "#000000"
    assert ir._color(node, "strokes") == "#00FF00"
    assert ir._color({"fills": [_solid("#123456"), {"type": "IMAGE"}]}) == "#123456"


def _node(node_id, node_type, x, y, w, h, **extra):
    return {"id": node_id, "name": node_id, "type": node_type,
            "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h}, **extra}


def _text_node(node_id, x, y, w, h, chars):
    return _node(node_id, "TEXT", x, y, w, h, characters=chars,
                 style={"fontFamily": "Inter", "fontSize": 16, "fontWeight": 400})


def test_full_width_frame_card_groups_descendants_by_media_side_and_narrow_frames_do_not():
    card = _node("c", "FRAME", 40, 20, 520, 200, fills=[_solid("#FFFFFF")], children=[
        _text_node("t", 200, 40, 200, 40, "Items"),
        _node("p", "RECTANGLE", 420, 40, 120, 120, fills=[{"type": "IMAGE", "imageRef": "r"}])])
    narrow = _node("n", "FRAME", 40, 260, 200, 100, strokes=[_solid("#000000")], children=[
        _text_node("t2", 50, 280, 100, 20, "Narrow")])
    band = _node("band", "FRAME", 0, 0, 600, 400, fills=[_solid("#F0F0F0")], children=[card, narrow])
    tree = _node("root", "FRAME", 0, 0, 600, 400, children=[band])
    result = ir.build_ir(tree)
    rows = result.bands[0].rows
    assert [r.card.node_id if r.card else None for r in rows] == ["c", None]
    assert [[e.node_id for e in cell.elements] for cell in rows[0].cells] == [["t"], ["p"]]
    assert rows[0].card.fill == "#FFFFFF"
    assert [d.node_id for d in result.decisions if d.rule == "card-group"] == ["c"]
    assert [d.node_id for d in result.decisions if d.rule == "card-frame"] == ["n"]


def _image_node(node_id, x, y, w, h):
    return _node(node_id, "RECTANGLE", x, y, w, h, fills=[{"type": "IMAGE", "imageRef": node_id}])


def _row_shape(result):
    return [[[e.node_id for e in cell.elements] for cell in row.cells] for row in result.bands[0].rows]


def test_full_band_backdrop_rectangle_is_not_a_card_and_keeps_normal_rows():
    band = _node("band", "FRAME", 0, 0, 600, 400, children=[
        _node("bg", "RECTANGLE", 0, 0, 600, 400, fills=[_solid("#EEEEEE")]),
        _text_node("h", 40, 20, 520, 40, "Heading"),
        _text_node("a", 40, 100, 240, 40, "A"),
        _text_node("b", 320, 100, 240, 40, "B"),
        _image_node("img", 40, 200, 520, 160)])
    result = ir.build_ir(_node("root", "FRAME", 0, 0, 600, 400, children=[band]))
    assert _row_shape(result) == [[["h"]], [["a"], ["b"]], [["img"]]]
    assert all(row.card is None for row in result.bands[0].rows)
    assert [d.node_id for d in result.decisions if d.rule == "card-background"] == ["bg"]
    assert not [d for d in result.decisions if d.rule in ("card-group", "card-frame")]


def test_full_band_backdrop_frame_is_not_a_card():
    backdrop = _node("bgf", "FRAME", 0, 0, 600, 400, fills=[_solid("#FFFFFF")], children=[
        _text_node("a", 40, 100, 240, 40, "A"), _text_node("b", 320, 100, 240, 40, "B")])
    band = _node("band", "FRAME", 0, 0, 600, 400, fills=[_solid("#F0F0F0")], children=[backdrop])
    result = ir.build_ir(_node("root", "FRAME", 0, 0, 600, 400, children=[band]))
    assert all(row.card is None for row in result.bands[0].rows)


def test_genuine_card_keeps_side_by_side_text_as_two_cells():
    card = _node("c", "FRAME", 40, 20, 520, 200, fills=[_solid("#FFFFFF")], children=[
        _text_node("a", 60, 40, 200, 40, "A"), _text_node("b", 320, 40, 200, 40, "B"),
        _text_node("c2", 60, 100, 200, 40, "Below A")])
    band = _node("band", "FRAME", 0, 0, 600, 400, fills=[_solid("#F0F0F0")], children=[card])
    result = ir.build_ir(_node("root", "FRAME", 0, 0, 600, 400, children=[band]))
    row = result.bands[0].rows[0]
    assert row.card is not None
    assert [[e.node_id for e in cell.elements] for cell in row.cells] == [["a", "c2"], ["b"]]


@pytest.mark.parametrize("band_type", ["INSTANCE", "COMPONENT"])
def test_non_frame_band_with_image_fill_keeps_background_and_live_text(band_type):
    band = node("band", band_type, (100, 200, 100, 100),
                fills=[{"type": "IMAGE", "imageRef": "hero"}], children=[
                    node("copy", "TEXT", (120, 220, 50, 20), characters="live")])
    result = ir.build_ir(node("frame", "FRAME", (100, 200, 100, 100), children=[band]))
    background = result.bands[0].background_image
    assert background is not None and background.node_id == "band" and background.export == "fill"
    elements = all_elements(result)
    assert [e.node_id for e in elements if isinstance(e, ir.Text)] == ["copy"]
    assert not any(isinstance(e, ir.Image) for e in elements)


@pytest.mark.parametrize("container_type", ["COMPONENT", "COMPONENT_SET"])
def test_component_container_flattens_to_live_texts(container_type):
    root = document(node("comp", container_type, (100, 200, 100, 100), children=[
        node("a", "TEXT", (100, 200, 50, 20), characters="A"),
        node("b", "TEXT", (100, 240, 50, 20), characters="B")]))
    result = ir.build_ir(root)
    elements = all_elements(result)
    assert [e.node_id for e in elements] == ["a", "b"]
    assert not any(isinstance(e, ir.Unknown) for e in elements)
    assert any(d.node_id == "comp" and d.rule == "flatten-container" for d in result.decisions)


def test_vector_flag_marks_vector_image_and_shared_helpers_are_public():
    root = document(node("logo", "GROUP", (100, 200, 10, 10), children=[
        node("v1", "VECTOR", (100, 200, 10, 10)), node("v2", "VECTOR", (105, 200, 5, 10))]),
        node("photo", "RECTANGLE", (100, 230, 20, 20), fills=[{"type": "IMAGE", "imageRef": "r"}]))
    images = {e.node_id: e for e in all_elements(ir.build_ir(root)) if isinstance(e, ir.Image)}
    assert images["logo"].vector is True and images["photo"].vector is False
    assert ir.is_hidden({"opacity": 0}) and ir.is_hidden({"visible": False})
    assert not ir.is_hidden({"opacity": 0.5, "visible": True})
    import emit
    assert emit.envelope is ir.envelope
