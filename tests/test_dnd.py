import dnd
import pytest

@pytest.mark.parametrize(
    ("widths", "expected"),
    [
        ([100], "1-column-full-width"),
        ([50, 50], "2-columns-equal-width"),
        ([25, 75], "2-columns-25%-75%"),
        ([33, 67], "2-columns-33%-67%"),
        ([67, 33], "2-columns-67%-33%"),
        ([75, 25], "2-columns-75%-25%"),
        ([33, 33, 33], "3-columns-equal-width"),
        ([25, 25, 50], "3-columns-25%-25%-50%"),
        ([25, 50, 25], "3-columns-25%-50%-25%"),
        ([50, 25, 25], "3-columns-50%-25%-25%"),
        ([25, 25, 25, 25], "4-columns-equal-width"),
    ],
)
def test_nearest_column_layout_uses_exact_presets(widths, expected):
    assert dnd.nearest_column_layout(widths) == expected


def test_nearest_column_layout_respects_fixed_content_and_falls_back():
    assert dnd.nearest_column_layout([61, 39]) == "2-columns-67%-33%"
    assert dnd.nearest_column_layout([260, 260], [300, 0]) == "2-columns-67%-33%"
    assert dnd.nearest_column_layout([61, 39], [1_000, 1_000]) == "2-columns-67%-33%"


def test_text_helpers_escape_markup_and_keep_paragraph_spacing_reset():
    assert dnd.p("<b>&") == '<p style="margin:0;padding-bottom:0">&lt;b&gt;&amp;</p>'
    assert dnd.p("copy", "font-size:20px") == (
        '<p style="margin:0;padding-bottom:0;font-size:20px">copy</p>'
    )
    assert dnd.h(2, "<b>&") == "<h2>&lt;b&gt;&amp;</h2>"


def test_image_requires_alt_text_and_button_preserves_padding():
    with pytest.raises(ValueError, match="alt text"):
        dnd.image({"id": "asset", "url": "https://example.invalid/image.jpg"}, "", 300)

    block = dnd.button(
        "Order now",
        "https://example.invalid/order",
        bg="#000000",
        color="#FFFFFF",
        font_family="Arial",
        font_size=16,
        font_weight="700",
        letter_spacing=0.78,
        inner=(1, 2, 3, 4),
        top=5,
        right=6,
        bottom=7,
        left=8,
    )
    styles = block["data"]["styles"]

    assert styles["letter_spacing"] == 1
    assert [styles[key] for key in (
        "inner_padding_top",
        "inner_padding_right",
        "inner_padding_bottom",
        "inner_padding_left",
    )] == [1, 2, 3, 4]
    assert [styles[key] for key in (
        "block_padding_top",
        "block_padding_right",
        "block_padding_bottom",
        "block_padding_left",
    )] == [5, 6, 7, 8]


def test_type_role_and_template_styles_preserve_style_contracts():
    node = {
        "style": {
            "fontSize": 20,
            "lineHeightPx": 27,
            "fontWeight": 400,
            "letterSpacing": 0.78,
            "textAlignHorizontal": "CENTER",
        },
        "fills": [{"type": "SOLID", "color": {"r": 0.1, "g": 0.2, "b": 0.3}}],
    }
    role = dnd.type_role(node, "Arial", mobile_px=18)

    assert role["line_height"] == 1.35
    assert role["mobile_line_height"] == 1.35
    assert role["mobile_font_size"] == 18
    overridden = dnd.type_role(node, "Arial", 18, color="#FFFFFF", letter_spacing=2)
    assert overridden["color"] == "#FFFFFF" and overridden["letter_spacing"] == 2

    styles = dnd.template_styles(
        background="#FFFFFF",
        text={"color": "#111111"},
        headings={1: {"font_size": 32}},
    )

    assert len(styles) == 8
    assert [style["style_type"] for style in styles] == [
        "base-styles",
        "text-styles",
        "link-styles",
        "heading-1-styles",
        "heading-2-styles",
        "heading-3-styles",
        "heading-4-styles",
        "mobile-styles",
    ]
    assert styles[3]["styles"]["margin_bottom"] == 0


def test_box_split_accounts_for_gaps_and_asymmetric_content_edges():
    assert dnd.split_widths_by_box([(380, 604, 147, 96), (40, 452, 300, 400)], 40, 560) == [320, 200]


def test_plain_type_role_preserves_desktop_mobile_and_overrides():
    assert dnd.type_role_values("Georgia", 56, "200", 36, line_height_px=64,
                                italic=True, letter_spacing=0.78, color="#123456",
                                align="center", margin_bottom=0) == {
        "font_family": "Georgia", "font_size": 56, "font_weight": "200", "font_style": "italic",
        "line_height": 1.14, "letter_spacing": 1, "color": "#123456", "text_align": "center",
        "mobile_font_size": 36, "mobile_line_height": 1.14, "margin_bottom": 0,
    }


def test_outline_button_border_is_optional():
    options = {"bg": "#FAFAF8", "color": "#000000", "font_family": "Arial", "font_size": 13,
               "font_weight": "600"}
    outlined = dnd.button("Shop", "https://example.invalid", border=("#000000", 1.2), **options)
    styles = outlined["data"]["styles"]
    assert styles["background_color"] == "#FAFAF8"
    assert (styles["border_color"], styles["border_width"], styles["border_style"]) == (
        "#000000", 1, "solid")
    assert not any(key in dnd.button("Shop", "https://example.invalid", **options)["data"]["styles"]
                   for key in ("border_color", "border_width", "border_style"))
