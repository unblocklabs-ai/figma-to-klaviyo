import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture
def sample_tree(tmp_path):
    """A synthetic email with live text, a photo, and a button."""
    def node(node_id, node_type, box, **properties):
        x, y, width, height = box
        return {"id": node_id, "name": node_id, "type": node_type,
                "absoluteBoundingBox": {"x": x, "y": y, "width": width, "height": height},
                **properties}

    text_style = {"fontFamily": "Arial", "fontSize": 18, "fontWeight": 400}
    tree = node("frame", "FRAME", (0, 0, 600, 400), children=[
        node("band", "FRAME", (0, 0, 600, 400), children=[
            node("photo", "RECTANGLE", (40, 20, 520, 160),
                 fills=[{"type": "IMAGE", "imageRef": "synthetic-photo", "scaleMode": "FILL"}]),
            node("copy", "TEXT", (40, 200, 520, 30), characters="A sample email.", style=text_style),
            node("button", "FRAME", (40, 260, 200, 40),
                 fills=[{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}], children=[
                     node("label", "TEXT", (60, 270, 160, 20),
                          characters="Learn more", style=text_style)]),
        ]),
    ])
    path = tmp_path / "sample-tree.json"
    path.write_text(json.dumps(tree))
    return path
