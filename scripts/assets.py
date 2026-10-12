"""Pure asset pipeline: IR → export plan, crop of overflowing renders, fill-stack compositing.

No network. Pillow only. Rendering math is documented on each function.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from emit import prepare_bands
from ir import IR, Box, Image
from PIL import Image as PILImage


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")


def _box(box: Box | None) -> dict | None:
    return None if box is None else {"x": box.x, "y": box.y, "w": box.w, "h": box.h}


def asset_plan(ir: IR, scale: int = 2, overrides: dict | None = None) -> list[dict]:
    """One entry per unique image node id emit will actually place (skips, off-canvas images, and
    columns beyond four are excluded): backgrounds first within each band, then content in reading
    order. Deterministic. size = round(visible box * scale)."""
    bands = prepare_bands(ir, overrides)
    plan: list[dict] = []
    seen: set[str] = set()
    colors = Counter(b.background_color for b in ir.bands if b.background_color)
    page_matte = colors.most_common(1)[0][0] if colors else "#FFFFFF"

    def add(image: Image, role: str, matte: str) -> None:
        if image.node_id in seen:
            return
        seen.add(image.node_id)
        visible = image.clipped_box or image.box
        fmt = "png" if image.export == "node" and not image.masked and image.vector else "jpg"
        stem = re.sub(r"[^A-Za-z0-9]+", "-", image.node_id).strip("-")
        slug = _slug(image.name_hint)
        plan.append({
            "node_id": image.node_id, "role": role, "export": image.export,
            "box": _box(image.box), "clipped_box": _box(image.clipped_box),
            "paints": image.paints if image.export == "fill" else [],
            "name_hint": image.name_hint,
            "filename": f"{stem}-{slug}.{fmt}" if slug else f"{stem}.{fmt}",
            "format": fmt,
            "size": [max(1, round(visible.w * scale)), max(1, round(visible.h * scale))],
            "matte": matte,
        })

    for band in bands:
        matte = band.background_color or page_matte
        if band.background_image is not None:
            add(band.background_image, "background", matte)
        for row in band.rows:
            for cell in row.cells:
                for element in cell.elements:
                    if isinstance(element, Image):
                        add(element, "content", matte)
    return plan


def _crop_rect(box: Box, clipped: Box, scale: float, limit: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = max(1, round(clipped.w * scale)), max(1, round(clipped.h * scale))
    left = min(round((clipped.x - box.x) * scale), max(0, limit[0] - width))
    top = min(round((clipped.y - box.y) * scale), max(0, limit[1] - height))
    return left, top, left + width, top + height


def crop_to_visible(img: PILImage.Image, box: Box, clipped_box: Box | None, scale: float = 2
                    ) -> PILImage.Image:
    """Crop a render of the node's full box (at scale) down to its visible clipped_box."""
    if clipped_box is None:
        return img
    return img.crop(_crop_rect(box, clipped_box, scale, img.size))


# ---------------------------------------------------------------------------
# composite_fill
# ---------------------------------------------------------------------------

def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _with_opacity(layer: PILImage.Image, opacity: float) -> PILImage.Image:
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda v: round(v * opacity)))
    return layer


def _stretch_layer(src: PILImage.Image, transform: list[list[float]], size: tuple[int, int]
                   ) -> PILImage.Image:
    """imageTransform [[A,B,tx],[C,D,ty]] maps normalized node (u,v) to normalized image space:
    s = (A*u + B*v + tx, C*u + D*v + ty). PIL AFFINE wants, per output pixel (x,y), the input pixel
    (a*x + b*y + c, d*x + e*y + f). With u = x/W, v = y/H and pixel = s * (Sw, Sh):
    a=A*Sw/W b=B*Sw/H c=tx*Sw d=C*Sh/W e=D*Sh/H f=ty*Sh (PIL samples at pixel centers itself)."""
    width, height = size
    sw, sh = src.size
    (a, b, tx), (c, d, ty) = transform
    coeffs = (a * sw / width, b * sw / height, tx * sw, c * sh / width, d * sh / height, ty * sh)
    # Pillow's transform does not prefilter: pre-reduce the source when it is sampled sparsely.
    fx, fy = math.hypot(coeffs[0], coeffs[3]), math.hypot(coeffs[1], coeffs[4])
    if max(fx, fy) > 1.5:
        k = math.floor(min(fx, fy))
        if k >= 2:
            nw, nh = max(1, round(sw / k)), max(1, round(sh / k))
            src = src.resize((nw, nh), PILImage.LANCZOS)
            rx, ry = nw / sw, nh / sh
            coeffs = (coeffs[0] * rx, coeffs[1] * rx, coeffs[2] * rx,
                      coeffs[3] * ry, coeffs[4] * ry, coeffs[5] * ry)
    return src.transform(size, PILImage.AFFINE, coeffs, resample=PILImage.BICUBIC)


def _image_layer(src: PILImage.Image, paint: dict, size: tuple[int, int], scale: float
                 ) -> PILImage.Image:
    width, height = size
    src = src.convert("RGBA")
    mode = paint.get("scaleMode", "FILL")
    layer = PILImage.new("RGBA", size, (0, 0, 0, 0))
    if mode == "STRETCH" and paint.get("imageTransform"):
        layer = _stretch_layer(src, paint["imageTransform"], size)
    elif mode == "STRETCH":
        layer = src.resize(size, PILImage.LANCZOS)  # no transform = identity mapping
    elif mode in {"FILL", "FIT"}:
        ratio = (min if mode == "FIT" else max)(width / src.width, height / src.height)
        fitted = src.resize((max(1, round(src.width * ratio)), max(1, round(src.height * ratio))),
                            PILImage.BICUBIC)
        layer.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    elif mode == "TILE":
        factor = paint.get("scalingFactor", 1) * scale
        tile = src.resize((max(1, round(src.width * factor)), max(1, round(src.height * factor))),
                          PILImage.BICUBIC)
        for y in range(0, height, tile.height):
            for x in range(0, width, tile.width):
                layer.paste(tile, (x, y))
    return _with_opacity(layer, paint.get("opacity", 1))


def _lookup(stops: list[dict], opacity: float) -> list[tuple[int, int, int, int]]:
    ordered = sorted(stops, key=lambda stop: stop["position"])
    table = []
    for i in range(256):
        t = i / 255
        if t <= ordered[0]["position"]:
            lo = hi = ordered[0]
            f = 0.0
        elif t >= ordered[-1]["position"]:
            lo = hi = ordered[-1]
            f = 0.0
        else:
            hi = next(stop for stop in ordered if stop["position"] >= t)
            lo = ordered[ordered.index(hi) - 1]
            span = hi["position"] - lo["position"]
            f = (t - lo["position"]) / span if span else 0.0
        c0, c1 = _hex(lo["color"]), _hex(hi["color"])
        a = (lo.get("alpha", 1) * (1 - f) + hi.get("alpha", 1) * f) * opacity
        table.append((*(round(c0[k] * (1 - f) + c1[k] * f) for k in range(3)), round(a * 255)))
    return table


def _gradient_layer(paint: dict, size: tuple[int, int], warnings: list[str]) -> PILImage.Image | None:
    handles = paint.get("gradientHandlePositions") or []
    stops = paint.get("gradientStops") or []
    if len(handles) < 2 or not stops:
        warnings.append(f"{paint['type']} missing handles or stops; skipped")
        return None
    radial = paint["type"] == "GRADIENT_RADIAL"
    if radial and len(handles) < 3:
        warnings.append("GRADIENT_RADIAL missing third handle; skipped")
        return None
    table = _lookup(stops, paint.get("opacity", 1))
    (x0, y0), (x1, y1) = [(h["x"], h["y"]) for h in handles[:2]]
    e1x, e1y = x1 - x0, y1 - y0
    if radial:
        e2x, e2y = handles[2]["x"] - x0, handles[2]["y"] - y0
        det = e1x * e2y - e1y * e2x
        if abs(det) < 1e-12:
            warnings.append("GRADIENT_RADIAL degenerate handles; skipped")
            return None
    else:
        norm = e1x * e1x + e1y * e1y
        if norm < 1e-12:
            warnings.append("GRADIENT_LINEAR degenerate handles; skipped")
            return None
    gw, gh = max(1, math.ceil(size[0] / 4)), max(1, math.ceil(size[1] / 4))
    data = []
    for j in range(gh):
        vy = (j + 0.5) / gh - y0
        for i in range(gw):
            vx = (i + 0.5) / gw - x0
            if radial:
                alpha = (vx * e2y - vy * e2x) / det
                beta = (e1x * vy - e1y * vx) / det
                t = math.hypot(alpha, beta)
            else:
                t = (vx * e1x + vy * e1y) / norm
            data.append(table[round(min(1.0, max(0.0, t)) * 255)])
    small = PILImage.new("RGBA", (gw, gh))
    small.putdata(data)
    return small.resize(size, PILImage.BILINEAR)


def composite_fill(sources: dict[str, PILImage.Image], paints: list[dict], box: Box,
                   scale: float = 2, clipped_box: Box | None = None,
                   matte: str = "#FFFFFF") -> tuple[PILImage.Image, list[str]]:
    """Render a fill stack bottom to top at box * scale; return (RGB image, warnings).
    The result is flattened onto matte and cropped to clipped_box when given."""
    size = (max(1, round(box.w * scale)), max(1, round(box.h * scale)))
    canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
    warnings: list[str] = []
    for index, paint in enumerate(paints):
        kind = paint.get("type")
        label = f"paint {index} ({kind})"
        if paint.get("blendMode", "NORMAL") not in {"NORMAL", "PASS_THROUGH"}:
            warnings.append(f"{label}: blendMode {paint['blendMode']} unsupported; rendered as NORMAL")
        layer: PILImage.Image | None = None
        if kind == "SOLID":
            r, g, b = _hex(paint.get("color", "#000000"))
            layer = PILImage.new("RGBA", size, (r, g, b, round(paint.get("opacity", 1) * 255)))
        elif kind == "IMAGE":
            if paint.get("filters"):
                warnings.append(f"{label}: image filters unsupported; ignored")
            source = sources.get(paint.get("imageRef", ""))
            if source is None:
                warnings.append(f"{label}: missing source image {paint.get('imageRef')}; skipped")
            else:
                layer = _image_layer(source, paint, size, scale)
        elif kind in {"GRADIENT_LINEAR", "GRADIENT_RADIAL"}:
            layer = _gradient_layer(paint, size, warnings)
        else:
            warnings.append(f"{label}: unsupported paint type; skipped")
        if layer is not None:
            canvas = PILImage.alpha_composite(canvas, layer)
    flat = PILImage.new("RGB", size, _hex(matte))
    flat.paste(canvas, mask=canvas.getchannel("A"))
    return crop_to_visible(flat, box, clipped_box, scale), warnings


def save_asset(img: PILImage.Image, path: str | Path, fmt: str, matte: str = "#FFFFFF") -> None:
    """JPEG q85 optimized (flattened on matte), or optimized PNG keeping alpha."""
    if fmt == "jpg":
        if img.mode in {"RGBA", "LA", "P"}:
            rgba = img.convert("RGBA")
            flat = PILImage.new("RGB", rgba.size, _hex(matte))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        img.convert("RGB").save(path, "JPEG", quality=85, optimize=True)
    else:
        img.save(path, "PNG", optimize=True)
