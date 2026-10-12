import copy
import html
import re
from dataclasses import replace

import emit
import f2k
import ir
import pytest


def elements(result):
    return [e for b in result.bands for r in b.rows for c in r.cells for e in c.elements]


def sections(body):
    return body["data"]["attributes"]["definition"]["body"]["sections"]


def blocks(body):
    return [block for section in sections(body) for row in section["rows"]
            for column in row["columns"] for block in column["blocks"]]


def styles(body):
    return {s["style_type"]: s["styles"] for s in body["data"]["attributes"]["definition"]["styles"]}


def normalize(text):
    return " ".join(text.split())


def visible_text(content):
    content = re.sub(r"<br\s*/?>", " ", content, flags=re.IGNORECASE)
    return normalize(html.unescape(re.sub(r"<[^>]+>", "", content)))


def body_text(body):
    return normalize(" ".join(visible_text(b["data"].get("content", "")) for b in blocks(body)))


def simple_text(node_id, size=20, characters="Live copy", **changes):
    style = ir.TextStyle("Serif", None, size, size * 1.2, 400, False, 0, "left", "#000000")
    return ir.Text(node_id, ir.Box(20, 20, 100, 24), characters, replace(style, **changes), [])


def simple_ir(*rows, background=None, padding=(0, 20, 0, 20), color="#FFFFFF"):
    band = ir.Band("band", "Band", ir.Box(0, 0, 600, 400), color, background, padding, list(rows))
    return ir.IR("frame", "Test", 600, 400, [band], [])


def single(element):
    return ir.Row(element.box, [ir.Cell(element.box, [element])])


def test_band_gaps_have_one_section_padding_owner_before_the_footer():
    bands = []
    for i, y in enumerate((24, 164, 304)):
        intro = replace(simple_text(f"intro-{i}"), box=ir.Box(20, y + 10, 560, 24))
        pair = [replace(simple_text(f"column-{i}-{j}"), box=ir.Box(20 + j * 300, y + 50, 260, 24))
                for j in range(2)]
        row = ir.Row(ir.Box(20, y + 50, 560, 24), [ir.Cell(t.box, [t]) for t in pair])
        bands.append(ir.Band(f"band-{i}", "Band", ir.Box(0, y, 600, 100), "#FFFFFF", None,
                             (0, 20, 0, 20), [single(intro), row]))
    conversion = emit.emit(ir.IR("frame", "Gaps", 600, 428, bands, []))
    content = sections(conversion.body)[:-1]
    assert len(content) == 6
    assert [s["data"]["styles"]["inner_padding_top"] for s in content] == [34, 16, 50, 16, 50, 16]
    assert [s["data"]["styles"]["inner_padding_bottom"] for s in content] == [0, 26, 0, 26, 0, 50]
    assert all(b["data"]["styles"]["block_padding_top"] == 0
               for s in content for r in s["rows"] for c in r["columns"] for b in c["blocks"])
    assert sections(conversion.body)[-1]["data"]["styles"]["inner_padding_top"] == 8
    assert sections(conversion.body)[-1]["data"]["styles"]["inner_padding_bottom"] == 32
    assert f2k.validate_body(conversion.body) == []


def test_overlapping_band_and_frame_gaps_clamp_and_record_decisions():
    bands = []
    for node_id, y in (("first", -24), ("second", 36)):
        text = replace(simple_text(node_id), box=ir.Box(20, y + 10, 100, 24))
        bands.append(ir.Band(node_id, "Band", ir.Box(0, y, 600, 100), "#FFFFFF", None,
                             (0, 20, 0, 20), [single(text)]))
    conversion = emit.emit(ir.IR("frame", "Overlap", 600, 112, bands, []))
    content = sections(conversion.body)[:-1]
    assert [s["data"]["styles"]["inner_padding_top"] for s in content] == [10, 10]
    assert [s["data"]["styles"]["inner_padding_bottom"] for s in content] == [66, 66]
    decisions = [d for d in conversion.decisions if d["rule"] == "overlap-gap"]
    assert [d["node_id"] for d in decisions] == ["first", "second", "second"]
    assert all(d["confidence"] == "low" for d in decisions)
    assert f2k.validate_body(conversion.body) == []


def test_type_role_character_weighting_merging_overflow_and_modal_exceptions():
    texts = [simple_text("body", 22, "A substantial dek wins over short repeated labels.")]
    texts += [simple_text(f"label-{i}", 13, "LABEL", family="Sans") for i in range(3)]
    texts += [simple_text(f"display-{size}", size, f"Headline {size}") for size in (80, 79, 60, 40, 30, 24)]
    texts += [simple_text("white", 60, "White headline", color="#FFFFFF", align="center", letterSpacing=0.5),
              simple_text("black", 60, "Black headline")]
    result = simple_ir(*(single(replace(t, box=ir.Box(20, i * 30, 100, 24))) for i, t in enumerate(texts)))
    conversion = emit.emit(result)
    role_styles = styles(conversion.body)
    assert role_styles["text-styles"]["font_size"] == 22
    assert [role_styles[f"heading-{level}-styles"]["font_size"] for level in range(1, 5)] == [80, 60, 40, 30]
    assert [d["node_id"] for d in conversion.decisions if d["rule"] == "heading-overflow"] == ["display-24"]
    white = next(b["data"]["content"] for b in blocks(conversion.body) if "White headline" in b["data"].get("content", ""))
    assert "color:#FFFFFF" in white and "letter-spacing:0.5px" in white and "text-align:center" in white
    merged = next(b["data"]["content"] for b in blocks(conversion.body) if "Headline 79" in b["data"].get("content", ""))
    assert merged.startswith("<h1") and "font-size" not in merged
    labels = [b["data"]["content"] for b in blocks(conversion.body) if "LABEL" in b["data"].get("content", "")]
    assert all(content.startswith("<p") and "font-size:13px" in content and "font-family:" in content for content in labels)
    assert any(d["rule"] == "inline-size" and d["node_id"] == "display-24" for d in conversion.decisions)
    assert sum(d["rule"] == "font-unmapped" for d in conversion.decisions) == 2
    assert f2k.validate_body(conversion.body) == []


def test_overrides_unknowns_escaping_and_brief_only_image_links():
    text = simple_text("copy", characters="old")
    unknown = ir.Unknown("unknown", ir.Box(20, 60, 80, 20), "STAR", "unsupported")
    image = ir.Image("image", ir.Box(20, 90, 100, 80), "node", "Do not use this name as alt")
    result = simple_ir(single(text), single(unknown), single(image))
    original = copy.deepcopy(result)
    default = emit.emit(result)
    assert "TODO:unmapped:unknown" in default.todos
    assert "TODO:link:image" not in default.todos
    overrides = {"skip": ["unknown"], "text": {"copy": '<Read & "edit">\nNext\u2028Line'},
                 "roles": {"copy": "h4"}, "links": {"image": "https://example.invalid/photo"},
                 "alt": {"image": "Brief alt"}, "ignored": "TODO:link:ignored"}
    assets = {"image": {"id": "asset", "url": "https://example.invalid/photo.png"}}
    conversion = emit.emit(result, overrides=overrides, assets=assets, name="Override")
    assert conversion.todos == [] and f2k.validate_body(conversion.body) == []
    copy_block = blocks(conversion.body)[0]["data"]["content"]
    assert copy_block.startswith("<h4") and "&lt;Read &amp;" in copy_block
    assert visible_text(copy_block) == '<Read & "edit"> Next Line'
    assert copy_block.count("<br>") == 2
    picture = next(b for b in blocks(conversion.body) if b["type"] == "image")
    assert picture["data"]["properties"]["href"] == "https://example.invalid/photo"
    assert {d["rule"] for d in conversion.decisions} >= {"skipped-override", "text-override", "role-override"}
    assert conversion.body["data"]["attributes"]["name"] == "Override"
    assert result == original


def test_mixed_segments_escape_html_and_keep_weight_italic_color_size():
    text = simple_text("copy", characters="A<&\nB")
    text = replace(text, segments=[ir.Segment("A", text.style), ir.Segment("<&\nB", replace(
        text.style, weight=700, italic=True, color="#123456", size=24))])
    content = blocks(emit.emit(simple_ir(single(text))).body)[0]["data"]["content"]
    assert '<span style="font-weight:700;font-style:italic;color:#123456;font-size:24px">&lt;&amp;<br>B</span>' in content
    assert visible_text(content) == "A<& B"


def test_clipped_geometry_controls_width_alignment_and_section_insets():
    image = ir.Image("photo", ir.Box(-100, -50, 800, 500), "node", "Photo",
                     clipped_box=ir.Box(40, 20, 520, 200))
    body = emit.emit(simple_ir(single(image), padding=(0, 0, 0, 0))).body
    section = sections(body)[0]
    assert section["data"]["styles"]["inner_padding_top"] == 20
    assert section["data"]["styles"]["inner_padding_bottom"] == 180
    assert section["data"]["styles"]["inner_padding_left"] == 40
    assert section["data"]["styles"]["inner_padding_right"] == 40
    image_styles = blocks(body)[0]["data"]["styles"]
    assert image_styles["width"] == 520 and image_styles["align"] == "center"


@pytest.mark.parametrize("clipped_box", [ir.Box(600, 20, 0, 80), ir.Box(20, 400, 100, 0)],
                         ids=["zero-width", "zero-height"])
def test_fully_clipped_images_are_dropped_without_losing_live_text(clipped_box):
    text = simple_text("live")
    image = ir.Image("offcanvas", ir.Box(600, 20, 100, 80), "node", "Photo", clipped_box=clipped_box)
    conversion = emit.emit(simple_ir(single(text), single(image), padding=(0, 0, 0, 0)))
    assert f2k.validate_body(conversion.body) == []
    assert text.characters in body_text(conversion.body)
    assert not any(b["type"] == "image" for b in blocks(conversion.body))
    assert conversion.todos == []
    decisions = [d for d in conversion.decisions if d["rule"] == "offcanvas-image"]
    assert len(decisions) == 1
    assert decisions[0]["node_id"] == image.node_id and decisions[0]["confidence"] == "low"


@pytest.mark.parametrize("side_padding", [300, 320], ids=["zero-width", "negative-width"])
def test_nonpositive_section_content_width_falls_back_to_band_edges(side_padding):
    text = simple_text("live")
    conversion = emit.emit(simple_ir(single(text), padding=(0, side_padding, 0, side_padding)))
    assert f2k.validate_body(conversion.body) == []
    assert text.characters in body_text(conversion.body)
    section_styles = sections(conversion.body)[0]["data"]["styles"]
    assert section_styles["inner_padding_left"] == section_styles["inner_padding_right"] == 0
    decisions = [d for d in conversion.decisions if d["rule"] == "section-width-fallback"]
    assert len(decisions) == 1 and decisions[0]["node_id"] == "band"


def test_background_sections_preserve_gaps_height_and_first_multi_row_options():
    background = ir.Image("background", ir.Box(0, 0, 600, 400), "fill", "Background")
    first = replace(simple_text("intro"), box=ir.Box(20, 20, 560, 24))
    a = replace(simple_text("left"), box=ir.Box(20, 74, 100, 24))
    b = replace(simple_text("right"), box=ir.Box(350, 74, 100, 24))
    row = ir.Row(ir.Box(20, 74, 430, 24), [ir.Cell(a.box, [a]), ir.Cell(b.box, [b])])
    conversion = emit.emit(simple_ir(single(first), row, background=background), brand={"footer": {"mode": "none"}})
    assert len(sections(conversion.body)) == 1
    section = sections(conversion.body)[0]
    section_styles = section["data"]["styles"]
    assert section_styles["column_align"] == "middle" and section_styles["column_direction"] == "ltr"
    assert section_styles["background_format"] == "cover" and section_styles["background_repeat"] is False
    assert section_styles["inner_padding_top"] == 20 and section_styles["inner_padding_bottom"] == 302
    assert section["rows"][0]["columns"][0]["blocks"][-1]["data"]["styles"]["block_padding_bottom"] == 30
    assert all(c["blocks"][0]["data"]["styles"]["block_padding_top"] == 0 for c in section["rows"][1]["columns"])
    assert 20 + 24 + 30 + 24 + 302 == 400
    assert f2k.validate_body(conversion.body) == []


def test_columns_overflow_keeps_first_four_and_records_omitted_ids():
    texts = [replace(simple_text(f"column-{i}"), box=ir.Box(i * 110 + 20, 20, 100, 24)) for i in range(5)]
    row = ir.Row(ir.Box(20, 20, 540, 24), [ir.Cell(t.box, [t]) for t in texts])
    conversion = emit.emit(simple_ir(row))
    assert len(sections(conversion.body)[0]["rows"][0]["columns"]) == 4
    assert "column-4" in next(d["detail"] for d in conversion.decisions if d["rule"] == "columns-overflow")
    assert f2k.validate_body(conversion.body) == []


def test_brand_controls_mobile_fonts_links_page_and_footer():
    text = simple_text("body", 17, "Body", family="Brand")
    heading = replace(simple_text("heading", 60, "Title", family="Brand"), box=ir.Box(20, 60, 200, 60))
    brand = {"page_background": "#000000", "font_map": {"Brand": {"stack": "Brand, Arial, sans-serif"}},
             "mobile": {"display_factor": 0.5, "body_factor": 0.5}, "link_color": "#123456"}
    result = simple_ir(single(text), single(heading), color=None)
    conversion = emit.emit(result, brand=brand)
    role_styles = styles(conversion.body)
    assert role_styles["text-styles"]["mobile_font_size"] == 16
    assert role_styles["heading-1-styles"]["mobile_font_size"] == 30
    assert role_styles["text-styles"]["font_family"] == "Brand, Arial, sans-serif"
    assert role_styles["link-styles"]["color"] == "#123456"
    assert sections(conversion.body)[0]["data"]["styles"]["background_color"] == "#000000"
    assert "color:#CCCCCC" in blocks(conversion.body)[-1]["data"]["content"]
    assert not any(d["rule"] == "font-unmapped" for d in conversion.decisions)
    brand["mobile"] = {"h1": 32, "text": 18}
    brand["footer"] = {"mode": "none"}
    without_footer = emit.emit(result, brand=brand)
    assert styles(without_footer.body)["heading-1-styles"]["mobile_font_size"] == 32
    assert styles(without_footer.body)["text-styles"]["mobile_font_size"] == 18
    assert len(blocks(without_footer.body)) == 2
    assert any(d["rule"] == "footer-none" for d in without_footer.decisions)


def test_todo_scanner_walks_all_strings_and_deduplicates_figma_instance_ids():
    body = {"TODO:asset:frame": [{"content": '<p>TODO:unmapped:I1:2;3:4 STAR</p>',
                                 "href": "TODO:link:1:2", "nested": ["TODO:link:1:2", "TODO:alt:logo"]}],
            "url": "https://todo.invalid/asset/1:2"}
    assert emit.find_todos(body) == ["TODO:alt:logo", "TODO:asset:frame", "TODO:link:1:2", "TODO:unmapped:I1:2;3:4"]


@pytest.mark.parametrize("encoded", ['<p>TODO&#58;link&#58;1:2</p>',
                                    '<p>TODO&#x3A;link&#x3A;1&#58;2</p>',
                                    '<a href="TODO%3Alink%3A1%3A2">CTA</a>',
                                    'TODO:link:1&#58;2', 'TODO:link:1%3A2',
                                    'TODO&#58;link%3A1%3A2',
                                    'TODO%26%2358%3Blink%26%2358%3B1%3A2'])
@pytest.mark.parametrize("as_key", [False, True], ids=["value", "key"])
def test_find_todos_decodes_markers_in_strings_and_keys(encoded, as_key):
    body = {encoded: ["TODO:alt:logo", "TODO:alt:logo"]} if as_key else {
        "nested": [encoded, "TODO:alt:logo", "TODO:alt:logo"]}
    assert emit.find_todos(body) == ["TODO:alt:logo", "TODO:link:1:2"]


@pytest.mark.parametrize("sizes,copies,families,expected_size,expected_stack", [
    ([16, 20], ["Same", "Same"], ["Sans", "Serif"], 20, "Georgia, 'Times New Roman', serif"),
    ([20, 20], ["Same", "Same"], ["Z Serif", "A Sans"], 20, "'Helvetica Neue', Helvetica, Arial, sans-serif"),
    ([30, 28], ["Much more text here", "Short"], ["Serif", "Serif"], 28, "Georgia, 'Times New Roman', serif"),
])
def test_body_role_ties_and_display_only_fallback(sizes, copies, families, expected_size, expected_stack):
    texts = [replace(simple_text(str(i), size, copy, family=family), box=ir.Box(20, i * 80 + 20, 300, 50))
             for i, (size, copy, family) in enumerate(zip(sizes, copies, families))]
    body = emit.emit(simple_ir(*(single(t) for t in texts))).body
    body_style = styles(body)["text-styles"]
    assert body_style["font_size"] == expected_size and body_style["font_family"] == expected_stack


def test_text_wrap_constraints_follow_column_insets_without_freezing_mobile_width():
    left = simple_text("left", characters="Left")
    center = replace(simple_text("center", characters="Center"), box=ir.Box(250, 60, 100, 24))
    right = replace(simple_text("right", characters="Right"), box=ir.Box(470, 100, 100, 24))
    body = emit.emit(simple_ir(single(left), single(center), single(right))).body
    contents = [b["data"]["content"] for b in blocks(body)[:3]]
    assert all("max-width:108px" in content and not re.search(r"(?<![-\w])width:", content) for content in contents)
    assert "margin-left:auto" not in contents[0]
    assert "margin-left:auto;margin-right:auto" in contents[1]
    assert "margin-left:auto" in contents[2] and "margin-right:auto" not in contents[2]


@pytest.mark.parametrize("rotation,quarter_turn", [
    (-90, True), (89, True), (90, True), (91, True),
    (-270, True), (269, True), (270, True), (271, True),
    (88.9, False), (91.1, False), (268.9, False), (271.1, False),
])
def test_quarter_turn_text_uses_own_alignment_not_rotated_box(rotation, quarter_turn):
    body = simple_text("body", characters="Longer body copy sets the left-aligned role.")
    label = replace(simple_text("label", characters="Label", align="right"),
                    box=ir.Box(500, 60, 44, 229), rotation_deg=rotation)
    content = blocks(emit.emit(simple_ir(single(body), single(label))).body)[1]["data"]["content"]
    assert "text-align:right" in content
    if quarter_turn:
        assert "max-width" not in content and "margin-left:auto" not in content and "margin-right:auto" not in content
    else:
        assert "max-width:52px" in content and "margin-left:auto" in content


def test_page_background_falls_back_to_last_band_then_white_and_brand_overrides():
    photo = ir.Image("i", ir.Box(0, 0, 600, 100), "fill", "")
    def band(node_id, y, h, color, image=None):
        return ir.Band(node_id, "", ir.Box(0, y, 600, h), color, image, (0, 0, 0, 0), [])
    def background(bands, brand=None):
        result = ir.IR("f", "x", 600, 1000, bands, [])
        body = emit.emit(result, brand=brand).body
        return body["data"]["attributes"]["definition"]["body"]["styles"]["background_color"]
    assert background([band("a", 0, 100, "#111111", photo), band("b", 100, 300, "#222222", photo)]) == "#222222"
    assert background([band("a", 0, 100, None)]) == "#FFFFFF"
    assert background([band("a", 0, 300, "#111111", photo), band("b", 300, 100, "#222222", photo)]) == "#222222"
    assert background([band("a", 0, 300, "#111111", photo), band("b", 300, 100, None, photo)]) == "#FFFFFF"
    assert background([band("a", 0, 500, "#111111", photo), band("b", 500, 100, "#222222")]) == "#222222"
    assert background([band("a", 0, 100, "#111111")], {"page_background": "#ABCDEF"}) == "#ABCDEF"


def _bare_ir(*bands):
    return ir.IR("root", "root", 600, 400, list(bands), [])


def _bare_band(node_id, color, photo):
    return ir.Band(node_id, node_id, ir.Box(0, 0, 600, 100), color, object() if photo else None, (0, 0, 0, 0), [])


def _page_background_detail(*bands):
    emitter = emit._Emitter(_bare_ir(*bands), {}, {}, {})
    color = emitter.page_background()
    return color, next(d["detail"] for d in emitter.decisions if d["rule"] == "page-background")


def test_page_background_decision_distinguishes_no_photo_free_band_from_no_color():
    color, detail = _page_background_detail(_bare_band("a", "#111111", True))
    assert color == "#111111" and "Every band has a photo" in detail
    color, detail = _page_background_detail(_bare_band("a", None, False), _bare_band("b", "#222222", True))
    assert color == "#222222" and "No photo-free band has a background color" in detail
    assert "Every band has a photo" not in detail
