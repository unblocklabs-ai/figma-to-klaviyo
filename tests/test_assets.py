import pytest
from assets import asset_plan, composite_fill, crop_to_visible, save_asset
from ir import Box, build_ir
from PIL import Image as PILImage

IDENTITY = [[1, 0, 0], [0, 1, 0]]


def quadrants(size=100):
    img = PILImage.new("RGB", (size, size))
    half = size // 2
    for (x, y), color in {(0, 0): (255, 0, 0), (half, 0): (0, 255, 0),
                          (0, half): (0, 0, 255), (half, half): (255, 255, 0)}.items():
        img.paste(color, (x, y, x + half, y + half))
    return img


def near(pixel, expected, tol=3):
    return all(abs(a - b) <= tol for a, b in zip(pixel, expected))


def test_crop_to_visible_keeps_correct_pixels():
    box, clipped = Box(-34, -28, 634, 844), Box(0, 0, 600, 800)
    img = PILImage.new("RGB", (1268, 1688))
    for x in range(0, 1268, 4):  # encode position in pixels
        for y in range(0, 1688, 4):
            img.paste((x // 8, y // 8, 0), (x, y, x + 4, y + 4))
    out = crop_to_visible(img, box, clipped, 2)
    assert out.size == (1200, 1600)
    assert out.getpixel((0, 0)) == img.getpixel((68, 56))
    assert out.getpixel((1199, 1599)) == img.getpixel((68 + 1199, 56 + 1599))
    assert crop_to_visible(img, box, None, 2) is img


@pytest.mark.parametrize("transform,expected", [
    ([[0.5, 0, 0], [0, 0.5, 0]], (255, 0, 0)),
    ([[0.5, 0, 0.5], [0, 0.5, 0.5]], (255, 255, 0)),
    ([[0.5, 0, 0.5], [0, 0.5, 0]], (0, 255, 0)),
    ([[0.5, 0, 0], [0, 0.5, 0.5]], (0, 0, 255)),
])
def test_stretch_crop_quadrants(transform, expected):
    paint = {"type": "IMAGE", "imageRef": "a", "scaleMode": "STRETCH", "imageTransform": transform}
    img, warnings = composite_fill({"a": quadrants()}, [paint], Box(0, 0, 40, 60), 2)
    assert warnings == [] and img.size == (80, 120) and img.mode == "RGB"
    for xy in [(10, 10), (70, 10), (10, 110), (70, 110), (40, 60)]:
        assert near(img.getpixel(xy), expected)


def test_stretch_rotation_maps_corners():
    # u,v -> image (1-v, u): rotates the image 90 degrees counterclockwise.
    paint = {"type": "IMAGE", "imageRef": "a", "scaleMode": "STRETCH",
             "imageTransform": [[0, -1, 1], [1, 0, 0]]}
    img, _ = composite_fill({"a": quadrants()}, [paint], Box(0, 0, 50, 50), 2)
    # node top-left (u=0,v=0) -> image (1,0): top-right = green
    assert near(img.getpixel((5, 5)), (0, 255, 0))
    # node top-right (u=1,v=0) -> image (1,1): bottom-right = yellow
    assert near(img.getpixel((94, 5)), (255, 255, 0))
    # node bottom-left (0,1) -> image (0,0): top-left = red
    assert near(img.getpixel((5, 94)), (255, 0, 0))
    # node bottom-right (1,1) -> image (0,1): bottom-left = blue
    assert near(img.getpixel((94, 94)), (0, 0, 255))


def test_solid_over_white_and_image_opacity():
    black20 = {"type": "SOLID", "color": "#000000", "opacity": 0.2}
    img, _ = composite_fill({}, [black20], Box(0, 0, 10, 10), 2)
    assert near(img.getpixel((3, 3)), (204, 204, 204), 1)
    red = PILImage.new("RGB", (8, 8), (255, 0, 0))
    paint = {"type": "IMAGE", "imageRef": "a", "scaleMode": "FILL", "opacity": 0.5}
    img, _ = composite_fill({"a": red}, [paint], Box(0, 0, 10, 10), 1)
    assert near(img.getpixel((5, 5)), (255, 127, 127), 2)


def test_fill_fit_and_tile_modes():
    wide = PILImage.new("RGB", (100, 50), (255, 0, 0))
    fill, _ = composite_fill({"a": wide}, [{"type": "IMAGE", "imageRef": "a", "scaleMode": "FILL"}],
                             Box(0, 0, 50, 50), 1)
    assert near(fill.getpixel((1, 1)), (255, 0, 0)) and near(fill.getpixel((48, 48)), (255, 0, 0))
    fit, _ = composite_fill({"a": wide}, [{"type": "IMAGE", "imageRef": "a", "scaleMode": "FIT"}],
                            Box(0, 0, 50, 50), 1)
    assert near(fit.getpixel((25, 25)), (255, 0, 0)) and near(fit.getpixel((25, 2)), (255, 255, 255))
    tile_src = quadrants(10)
    tiled, _ = composite_fill({"a": tile_src}, [{"type": "IMAGE", "imageRef": "a", "scaleMode": "TILE",
                                                  "scalingFactor": 1}], Box(0, 0, 20, 20), 1)
    assert near(tiled.getpixel((1, 1)), (255, 0, 0)) and near(tiled.getpixel((11, 1)), (255, 0, 0))
    assert near(tiled.getpixel((6, 1)), (0, 255, 0))


def test_radial_gradient_center_darker_than_corners():
    paint = {"type": "GRADIENT_RADIAL", "opacity": 1,
             "gradientHandlePositions": [{"x": 0.5, "y": 0.5}, {"x": 1, "y": 0.5}, {"x": 0.5, "y": 1}],
             "gradientStops": [{"color": "#000000", "alpha": 1, "position": 0},
                               {"color": "#000000", "alpha": 0, "position": 1}]}
    img, warnings = composite_fill({}, [paint], Box(0, 0, 100, 100), 2)
    assert warnings == []
    assert img.getpixel((100, 100))[0] < 10 and img.getpixel((2, 2))[0] > 250
    assert sum(img.getpixel((100, 100))) < sum(img.getpixel((3, 100)))


def test_linear_gradient_and_paint_opacity():
    paint = {"type": "GRADIENT_LINEAR", "opacity": 0.5,
             "gradientHandlePositions": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}],
             "gradientStops": [{"color": "#000000", "alpha": 0, "position": 0},
                               {"color": "#000000", "alpha": 1, "position": 1}]}
    img, _ = composite_fill({}, [paint], Box(0, 0, 100, 10), 1)
    assert img.getpixel((0, 5))[0] > 250 and 120 <= img.getpixel((99, 5))[0] <= 135


def test_unsupported_features_warn():
    paints = [{"type": "SOLID", "color": "#000000", "opacity": 0.2, "blendMode": "MULTIPLY"},
              {"type": "GRADIENT_ANGULAR", "opacity": 1},
              {"type": "IMAGE", "imageRef": "a", "scaleMode": "FILL", "filters": {"exposure": 1}}]
    img, warnings = composite_fill({"a": PILImage.new("RGB", (4, 4), (0, 0, 255))}, paints,
                                   Box(0, 0, 10, 10), 1)
    assert len(warnings) == 3
    assert "MULTIPLY" in warnings[0] and "GRADIENT_ANGULAR" in warnings[1] and "filters" in warnings[2]
    assert img.size == (10, 10)


def test_composite_fill_performance_and_crop():
    import time
    paint = {"type": "GRADIENT_RADIAL", "opacity": 0.42,
             "gradientHandlePositions": [{"x": 0.5, "y": 0.5}, {"x": 1, "y": 0.5}, {"x": 0.5, "y": 1}],
             "gradientStops": [{"color": "#000000", "alpha": 0, "position": 0},
                               {"color": "#000000", "alpha": 1, "position": 1}]}
    start = time.time()
    img, _ = composite_fill({}, [paint], Box(-10, 0, 620, 800), 2, Box(0, 0, 600, 800))
    assert img.size == (1200, 1600) and time.time() - start < 3


def test_save_asset_formats(tmp_path):
    rgba = PILImage.new("RGBA", (20, 10), (255, 0, 0, 0))
    save_asset(rgba, tmp_path / "a.png", "png")
    with PILImage.open(tmp_path / "a.png") as png:
        assert png.format == "PNG" and png.mode == "RGBA" and png.getpixel((1, 1))[3] == 0
    save_asset(rgba, tmp_path / "a.jpg", "jpg")
    with PILImage.open(tmp_path / "a.jpg") as jpg:
        assert jpg.format == "JPEG" and jpg.mode == "RGB" and near(jpg.getpixel((5, 5)), (255, 255, 255))


def test_jpeg_flattens_on_matte_and_png_keeps_alpha(tmp_path):
    img = PILImage.new("RGBA", (40, 40), (0, 0, 0, 0))
    img.paste((200, 30, 30, 255), (10, 10, 30, 30))
    save_asset(img, tmp_path / "a.jpg", "jpg", "#FAFAF8")
    with PILImage.open(tmp_path / "a.jpg") as out:
        assert near(out.convert("RGB").getpixel((1, 1)), (250, 250, 248), 3)
    save_asset(img, tmp_path / "a.png", "png", "#FAFAF8")
    with PILImage.open(tmp_path / "a.png") as out:
        assert out.mode == "RGBA" and out.getpixel((1, 1))[3] == 0


def test_composite_fill_flattens_on_matte():
    flat, _ = composite_fill({}, [], Box(0, 0, 10, 10), 1, matte="#FAFAF8")
    assert flat.getpixel((5, 5)) == (250, 250, 248)


def test_stretch_without_transform_is_plain_resize():
    src = PILImage.new("RGB", (100, 50))
    src.paste((255, 0, 0), (0, 0, 10, 50))
    src.paste((0, 0, 255), (90, 0, 100, 50))
    flat, _ = composite_fill({"r": src}, [{"type": "IMAGE", "imageRef": "r", "scaleMode": "STRETCH"}],
                             Box(0, 0, 50, 50), 1)
    assert near(flat.getpixel((0, 25)), (255, 0, 0), 10)
    assert near(flat.getpixel((49, 25)), (0, 0, 255), 10)


def test_stretch_downscale_is_prefiltered():
    src = PILImage.new("L", (3000, 3000))
    src.putdata([255 if (i + i // 3000) % 2 else 0 for i in range(3000 * 3000)])
    flat, _ = composite_fill({"r": src}, [{"type": "IMAGE", "imageRef": "r", "scaleMode": "STRETCH",
                                           "imageTransform": IDENTITY}], Box(0, 0, 1000, 1000), 1)
    values = [flat.getpixel((x, 500))[0] for x in range(100, 110)]
    assert all(abs(v - 127) <= 20 for v in values), values


# ---------------------------------------------------------------------------
# The plan must equal the set of images emit actually asks for.
# ---------------------------------------------------------------------------

def _n(node_id, node_type, box, **properties):
    x, y, width, height = box
    return {"id": node_id, "name": node_id, "type": node_type,
            "absoluteBoundingBox": {"x": x, "y": y, "width": width, "height": height}, **properties}


def _photo(node_id, box):
    return _n(node_id, "RECTANGLE", box, fills=[{"type": "IMAGE", "imageRef": "r" + node_id}])


def _frame(*children, height=200):
    return _n("1:0", "FRAME", (0, 0, 600, height), children=[
        _n("band", "FRAME", (0, 0, 600, height), children=list(children))])


def asset_todos(ir, overrides=None):
    from emit import emit, find_todos
    return {m.split(":", 2)[2] for m in find_todos(emit(ir, overrides=overrides).body)
            if m.startswith("TODO:asset:")}


SYNTHETIC = {
    "offcanvas": (_frame(_photo("2:1", (700, 10, 50, 50)), _photo("2:2", (0, 100, 100, 50))), None),
    "columns-overflow": (_frame(*(_photo(f"3:{i}", (i * 110, 10, 100, 100)) for i in range(5))), None),
    "skipped": (_frame(_photo("4:0", (0, 10, 100, 50)), _photo("4:1", (0, 100, 100, 50))),
                {"skip": ["4:1"]}),
}


@pytest.mark.parametrize("case", sorted(SYNTHETIC))
def test_plan_excludes_images_emit_drops(case):
    tree, overrides = SYNTHETIC[case]
    ir = build_ir(tree)
    planned = {e["node_id"] for e in asset_plan(ir, overrides=overrides)}
    assert planned == asset_todos(ir, overrides)
    dropped = {"offcanvas": "2:1", "columns-overflow": "3:4", "skipped": "4:1"}[case]
    assert dropped not in planned and planned
