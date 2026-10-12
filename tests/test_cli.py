import json
import sys
from collections import Counter
from pathlib import Path

import f2k
import pytest
from emit import emit, find_todos
from ir import Button, Image, build_ir
from PIL import Image as PilImage

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "tests/fixtures/brand.json"
PLACEHOLDER_HREFS = ["", "#", "javascript:void(0)", "JAVASCRIPT:alert(1)",
                     "https://example.com/", "https://www.example.org/", "https://EXAMPLE.NET/",
                     "https://brand.example/", "https://brand.invalid/", "https://brand.test/",
                     "https://brand.localhost/", "http://localhost:8080/", "TODO-approved-link",
                     "https://[bad", "http://", "https://"]


@pytest.mark.parametrize("href", ["https://[bad", "http://", "https://"])
def test_placeholder_hrefs_reports_malformed_urls(href):
    assert f2k.placeholder_hrefs({"href": href}) == [href]


@pytest.mark.parametrize("href", ["[unsubscribe_tag]", "[manage_preferences_tag]"])
def test_placeholder_hrefs_allows_rendered_klaviyo_tags(href):
    assert f2k.placeholder_hrefs(f'<a href="{href}">Footer</a>') == []


@pytest.fixture(autouse=True)
def loggie_calls(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("offline CLI test attempted a Loggie call")

    monkeypatch.setattr(f2k, "loggie", forbidden)
    return calls


@pytest.fixture
def cli(monkeypatch, capsys):
    def run(*args):
        monkeypatch.setattr(sys, "argv", ["f2k.py", *map(str, args)])
        code = 0
        try:
            f2k.main()
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            elif exc.code is not None:
                code = 1
                print(exc.code, file=sys.stderr)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def test_convert_handoff_and_completed_round_trip(cli, tmp_path, sample_tree):
    tree = sample_tree
    source = build_ir(json.loads(tree.read_text()))
    out = tmp_path / "draft"
    code, stdout, stderr = cli("convert", "--tree", tree, "--frame", source.frame_id,
                               "--brand", BRAND, "--out", out, "--name", "CLI draft")
    assert code == 0, stderr
    assert len(stdout.splitlines()) == 1
    summary = json.loads(stdout)
    filenames = {"template_body.json", "decisions.json", "todo.md", "overrides.todo.json"}
    assert {p.name for p in out.iterdir()} == filenames
    assert summary["outputs"] == {name: str(out / name) for name in filenames}
    body = json.loads((out / "template_body.json").read_text())
    todos = find_todos(body)
    expected_counts = dict.fromkeys(("link", "asset", "alt", "unmapped"), 0)
    expected_counts.update(Counter(marker.split(":", 2)[1] for marker in todos))
    assert summary["todos"] == expected_counts
    assert sum(summary["todos"].values()) == len(todos) > 0
    assert summary["push"] == "blocked until TODOs are resolved"
    sections = body["data"]["attributes"]["definition"]["body"]["sections"]
    assert summary["sections"] == len(sections)
    assert summary["blocks"] == Counter(block["type"] for section in sections for row in section["rows"]
                                        for column in row["columns"] for block in column["blocks"])
    assert body["data"]["attributes"]["name"] == "CLI draft"
    decisions = json.loads((out / "decisions.json").read_text())
    assert summary["decisions"] == {confidence: sum(d["confidence"] == confidence for d in decisions)
                                     for confidence in ("high", "medium", "low")}
    assert not any(d["rule"] == "font-unmapped" for d in decisions)
    worksheet = json.loads((out / "overrides.todo.json").read_text())
    report = (out / "todo.md").read_text()
    assert report.count("## Needs review") == 1
    for decision in decisions:
        if decision["confidence"] == "low":
            review = report.split("## Needs review", 1)[1]
            assert all(decision[key] in review for key in ("node_id", "rule", "detail"))
    for kind in expected_counts:
        assert f"## {kind}" in report
    contexts = {element.node_id: element for band in source.bands for row in band.rows
                for cell in row.cells for element in cell.elements}
    backgrounds = {band.background_image.node_id: band for band in source.bands if band.background_image}
    for marker in todos:
        _, kind, node_id = marker.split(":", 2)
        line = next(line for line in report.splitlines() if marker in line)
        if node_id in backgrounds:
            assert f"section background of band {backgrounds[node_id].name}" in line
            element = backgrounds[node_id].background_image
        else:
            element = contexts[node_id]
        if isinstance(element, Button):
            assert element.label.characters in line
        elif isinstance(element, Image):
            box = element.clipped_box or element.box
            assert element.name_hint in line
            assert f"{round(box.w)}×{round(box.h)}" in line
        if kind in ("link", "alt"):
            assert worksheet["links" if kind == "link" else "alt"][node_id] == ""
    assert all(set(worksheet[key]) == {m.split(":", 2)[2] for m in todos if m.startswith(f"TODO:{kind}:")}
               for kind, key in (("link", "links"), ("alt", "alt")))

    worksheet["links"] = {node_id: "https://www.klaviyo.com/" for node_id in worksheet["links"]}
    worksheet["alt"] = {node_id: "Approved test alt text" for node_id in worksheet["alt"]}
    overrides_path = out / "overrides.todo.json"
    overrides_path.write_text(json.dumps(worksheet))
    assets_path = tmp_path / "assets.json"
    assets_path.write_text(json.dumps({marker.split(":", 2)[2]: {
        "id": f"asset-{marker.split(':', 2)[2]}", "url": "https://example.invalid/photo.png"}
        for marker in todos if marker.startswith("TODO:asset:")}))
    complete = tmp_path / "complete"
    code, stdout, stderr = cli("convert", "--tree", tree, "--brand", BRAND, "--overrides", overrides_path,
                               "--assets", assets_path, "--out", complete)
    assert code == 0, stderr
    summary = json.loads(stdout)
    assert sum(summary["todos"].values()) == 0 and "push" not in summary
    body_path = complete / "template_body.json"
    assert find_todos(json.loads(body_path.read_text())) == []
    code, stdout, stderr = cli("validate", "--body", body_path)
    assert code == 0, stderr
    assert json.loads(stdout)["schema_errors"] == 0


def test_convert_rounds_visible_image_sizes_in_todo_report(cli, tmp_path):
    tree = {"id": "frame", "name": "Fractional image", "type": "FRAME",
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 600, "height": 600},
            "children": [{"id": "image", "name": "Product photo", "type": "RECTANGLE",
                          "absoluteBoundingBox": {"x": 20, "y": 20, "width": 276.914, "height": 412.484},
                          "fills": [{"type": "IMAGE", "imageRef": "photo", "scaleMode": "FILL"}]}]}
    tree_path = tmp_path / "tree.json"
    tree_path.write_text(json.dumps(tree))
    out = tmp_path / "draft"
    code, _stdout, stderr = cli("convert", "--tree", tree_path, "--brand", BRAND, "--out", out)
    assert code == 0, stderr
    report = (out / "todo.md").read_text()
    for marker in ("TODO:asset:image", "TODO:alt:image"):
        line = next(line for line in report.splitlines() if marker in line)
        assert 'image "Product photo" (visible 277×412 px)' in line


@pytest.fixture
def draft_body(sample_tree):
    source = build_ir(json.loads(sample_tree.read_text()))
    return emit(source, brand=json.loads(BRAND.read_text())).body


@pytest.fixture
def completed_body(draft_body, sample_tree):
    source = build_ir(json.loads(sample_tree.read_text()))
    todos = find_todos(draft_body)
    overrides = {"links": {marker.split(":", 2)[2]: "https://www.klaviyo.com/"
                           for marker in todos if marker.startswith("TODO:link:")},
                 "alt": {marker.split(":", 2)[2]: "Synthetic product photo"
                         for marker in todos if marker.startswith("TODO:alt:")}}
    assets = {marker.split(":", 2)[2]: {"id": "approved-image", "url": "https://cdn.klaviyo.com/photo.png"}
              for marker in todos if marker.startswith("TODO:asset:")}
    return emit(source, brand=json.loads(BRAND.read_text()), overrides=overrides, assets=assets).body


@pytest.fixture
def link_block(completed_body, block_type):
    sections = completed_body["data"]["attributes"]["definition"]["body"]["sections"]
    return next(block["data"] for section in sections for row in section["rows"]
                for column in row["columns"] for block in column["blocks"] if block["type"] == block_type)


@pytest.mark.parametrize("allow_todo,invalid_schema,expected", [(False, False, 1), (True, False, 0),
                                                               (False, True, 1), (True, True, 1)])
def test_validate_todo_gate_and_schema_errors(cli, tmp_path, draft_body, allow_todo, invalid_schema, expected):
    if invalid_schema:
        draft_body["data"]["type"] = "not-a-template"
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(draft_body))
    code, stdout, stderr = cli("validate", "--body", body_path, *(["--allow-todo"] if allow_todo else []))
    assert code == expected, stderr
    report = json.loads(stdout)
    assert bool(report["schema_errors"]) == invalid_schema
    assert report["todo_markers"] == {"count": len(find_todos(draft_body)), "markers": find_todos(draft_body)}


@pytest.mark.parametrize("template_id", [None, "existing-template"])
def test_push_rejects_todos_before_any_network_call(cli, tmp_path, draft_body, loggie_calls, template_id):
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(draft_body))
    args = ["push", "--slug", "offline", "--body", body_path, "--expect-account", "offline-account"]
    if template_id:
        args += ["--template-id", template_id]
    code, stdout, stderr = cli(*args)
    assert code != 0
    assert all(marker in stdout + stderr for marker in find_todos(draft_body))
    assert loggie_calls == []


@pytest.mark.parametrize("template_id", [None, "existing-template"], ids=["post", "patch"])
@pytest.mark.parametrize("block_type", ["button", "image", "text"])
@pytest.mark.parametrize("href", PLACEHOLDER_HREFS)
def test_push_rejects_placeholder_links_before_any_network_call(
        cli, tmp_path, completed_body, link_block, loggie_calls, template_id, block_type, href):
    if block_type == "text":
        link_block["content"] = f'<p><a href="{href}">CTA</a></p>'
    else:
        link_block["properties"]["href"] = href
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(completed_body))
    args = ["push", "--slug", "offline", "--body", body_path, "--expect-account", "offline-account"]
    if template_id:
        args += ["--template-id", template_id]
    code, stdout, stderr = cli(*args)
    assert code != 0, stderr
    assert json.loads(stdout)["placeholder_links"] == [href]
    assert loggie_calls == []


@pytest.mark.parametrize("command,template_id", [("validate", None), ("push", None),
                                                  ("push", "existing-template")],
                         ids=["validate", "post", "patch"])
@pytest.mark.parametrize("content,placeholder", [
    ("<a href=#>Shop</a>", "#"),
    ("<a href=https://brand.invalid/>Shop</a>", "https://brand.invalid/"),
    ("<A HREF='https://example.com'>Shop</A>", "https://example.com"),
    ("<a href>Shop</a>", ""),
    ("<link href=#>", "#"),
    ('<area HREF="&#35;" />', "#"),
    ("<a href=[unsubscribe_tag]>Unsubscribe</a>", None),
    ("<a href=https://www.klaviyo.com/>Shop</a>", None),
    ("<a href=mailto:help@klaviyo.com>Email</a>", None),
])
def test_html_href_validation_and_push(
        cli, tmp_path, monkeypatch, completed_body, command, template_id, content, placeholder):
    sections = completed_body["data"]["attributes"]["definition"]["body"]["sections"]
    block = next(block for section in sections for row in section["rows"]
                 for column in row["columns"] for block in column["blocks"] if block["type"] == "text")
    block["data"]["content"] = content
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(completed_body))
    calls = []

    def loggie(slug, method, path, **kwargs):
        calls.append((method, path))
        if path == "/accounts":
            return {"data": [{"id": "offline-account"}]}
        return {"data": {"id": "created-template"}}

    monkeypatch.setattr(f2k, "loggie", loggie)
    args = [command, "--body", body_path]
    if command == "push":
        args += ["--slug", "offline", "--expect-account", "offline-account"]
        if template_id:
            args += ["--template-id", template_id]
    code, stdout, stderr = cli(*args)
    if placeholder is not None:
        assert code == 1, stderr
        assert json.loads(stdout)["placeholder_links"] == [placeholder]
        assert calls == []
    else:
        assert code == 0, stderr
        if command == "validate":
            assert json.loads(stdout)["placeholder_links"] == []
            assert calls == []
        else:
            assert calls == [("GET", "/accounts"), ("PATCH", f"/templates/{template_id}")
                             if template_id else ("POST", "/templates")]


@pytest.mark.parametrize("content", ['<a href="#">Shop</a><![bogus]>',
                                     '<a href="#">Shop</a><![invalid[content]]>'])
def test_placeholder_hrefs_discards_malformed_html(content):
    assert f2k.placeholder_hrefs({"href": "https://brand.invalid/", "content": content}) == [
        "https://brand.invalid/"]


@pytest.mark.parametrize("allow_todo,invalid_schema,expected", [(False, False, 1), (True, False, 0),
                                                               (False, True, 1), (True, True, 1)])
def test_validate_lists_placeholder_links(
        cli, tmp_path, completed_body, allow_todo, invalid_schema, expected):
    sections = completed_body["data"]["attributes"]["definition"]["body"]["sections"]
    blocks = [block for section in sections for row in section["rows"]
              for column in row["columns"] for block in column["blocks"]]
    next(block for block in blocks if block["type"] == "button")["data"]["properties"]["href"] = ""
    next(block for block in blocks if block["type"] == "image")["data"]["properties"]["href"] = "#"
    next(block for block in blocks if block["type"] == "text")["data"]["content"] = "".join(
        f'<a href="{href}">CTA</a>' for href in PLACEHOLDER_HREFS[2:])
    if invalid_schema:
        completed_body["data"]["type"] = "not-a-template"
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(completed_body))
    code, stdout, stderr = cli("validate", "--body", body_path, *(["--allow-todo"] if allow_todo else []))
    assert code == expected, stderr
    report = json.loads(stdout)
    assert report["placeholder_links"] == sorted(PLACEHOLDER_HREFS)
    assert report["todo_markers"] == {"count": 0, "markers": []}
    assert bool(report["schema_errors"]) == invalid_schema


@pytest.mark.parametrize("template_id", [None, "existing-template"], ids=["post", "patch"])
@pytest.mark.parametrize("block_type", ["button", "image", "text"])
@pytest.mark.parametrize("href", ["mailto:help@klaviyo.com", "tel:+18005550100",
                                  "{% unsubscribe_link %}", "{% manage_preferences_link %}",
                                  "{{ organization.url }}", "https://notexample.com/",
                                  "https://www.klaviyo.com/?ref=example.com"])
def test_push_allows_resolved_links_and_standard_footer(
        cli, tmp_path, monkeypatch, completed_body, link_block, template_id, block_type, href):
    if block_type == "text":
        link_block["content"] = f'<p><a href="{href}">CTA</a></p>'
    else:
        link_block["properties"]["href"] = href
    footer = completed_body["data"]["attributes"]["definition"]["body"]["sections"][-1]
    content = footer["rows"][0]["columns"][0]["blocks"][0]["data"]["content"]
    assert "{% unsubscribe 'Unsubscribe' %}" in content
    assert "{% manage_preferences 'Manage preferences' %}" in content
    calls = []

    def loggie(slug, method, path, **kwargs):
        calls.append((method, path))
        if path == "/accounts":
            return {"data": [{"id": "offline-account"}]}
        return {"data": {"id": "created-template"}}

    monkeypatch.setattr(f2k, "loggie", loggie)
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(completed_body))
    args = ["push", "--slug", "offline", "--body", body_path, "--expect-account", "offline-account"]
    if template_id:
        args += ["--template-id", template_id]
    code, stdout, stderr = cli(*args)
    assert code == 0, stderr
    assert json.loads(stdout)["template_id"] == (template_id or "created-template")
    assert calls == [("GET", "/accounts"), ("PATCH", f"/templates/{template_id}")
                     if template_id else ("POST", "/templates")]


def test_convert_accepts_the_gutter_brand_key(cli, tmp_path, sample_tree):
    brand = tmp_path / "brand.json"
    brand.write_text(json.dumps({"gutter": 24}))
    tree = sample_tree
    code, _stdout, stderr = cli("convert", "--tree", tree, "--brand", brand, "--out", tmp_path / "out")
    assert code == 0, stderr
    body = json.loads((tmp_path / "out/template_body.json").read_text())
    styles = body["data"]["attributes"]["definition"]["body"]["sections"][0]["data"]["styles"]
    assert styles["inner_padding_left"] == styles["inner_padding_right"] == 24


@pytest.mark.parametrize("quote", ['"', ""], ids=["quoted", "unquoted"])
def test_check_reports_placeholder_links(cli, tmp_path, monkeypatch, quote):
    doc = "".join(f'<a href={quote}{href}{quote}>CTA</a>' for href in PLACEHOLDER_HREFS)
    doc += '<a href="mailto:help@klaviyo.com">Email</a><a href="{{ organization.url }}">Home</a>'
    doc += '<a href="[unsubscribe_tag]">Unsubscribe</a><a href="[manage_preferences_tag]">Preferences</a>'
    doc += '<a href="https://www.klaviyo.com/?ref=example.com">Shop</a>'
    monkeypatch.setattr(f2k, "loggie", lambda *args, **kwargs: {"data": {"attributes": {"html": doc}}})
    monkeypatch.setattr(f2k, "chrome", lambda: None)
    code, stdout, stderr = cli("check", "--slug", "offline", "--template-id", "template", "--out", tmp_path)
    assert code == 0, stderr
    assert json.loads(stdout)["placeholder_links"] == sorted(PLACEHOLDER_HREFS)
    assert json.loads((tmp_path / "check.json").read_text())["placeholder_links"] == sorted(PLACEHOLDER_HREFS)


@pytest.mark.parametrize("template_id", [None, "existing-template"], ids=["post", "patch"])
@pytest.mark.parametrize("content", ['<p>TODO&#58;link&#58;1:2</p>',
                                     '<p><a href="TODO%3Alink%3A1%3A2">CTA</a></p>'])
def test_push_rejects_encoded_todos_before_any_network_call(
        cli, tmp_path, completed_body, loggie_calls, template_id, content):
    sections = completed_body["data"]["attributes"]["definition"]["body"]["sections"]
    block = next(block for section in sections for row in section["rows"]
                 for column in row["columns"] for block in column["blocks"] if block["type"] == "text")
    block["data"]["content"] = content
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(completed_body))
    args = ["push", "--slug", "offline", "--body", body_path, "--expect-account", "offline-account"]
    if template_id:
        args += ["--template-id", template_id]
    code, stdout, stderr = cli(*args)
    assert code != 0, stderr
    assert json.loads(stdout)["todo_markers"] == {"count": 1, "markers": ["TODO:link:1:2"]}
    assert loggie_calls == []


@pytest.mark.parametrize("option,value,expected", [
    ("--brand", {"font_mpa": {}}, "unknown brand keys: font_mpa"),
    ("--overrides", {"alts": {}}, "unknown overrides keys: alts"),
    ("--frame", "not-in-tree", "frame not-in-tree not found"),
])
def test_convert_rejects_unknown_keys_and_missing_frame(cli, tmp_path, sample_tree, option, value, expected):
    argument = value
    if isinstance(value, dict):
        argument = tmp_path / "invalid.json"
        argument.write_text(json.dumps(value))
    out = tmp_path / "draft"
    code, stdout, stderr = cli("convert", "--tree", sample_tree,
                               option, argument, "--out", out)
    assert code != 0 and expected in stdout + stderr
    assert not out.exists()


@pytest.mark.parametrize("option", ["--tree", "--brand", "--overrides", "--assets"])
@pytest.mark.parametrize("missing", [True, False], ids=["missing-file", "bad-json"])
def test_convert_reports_input_file_errors(cli, tmp_path, sample_tree, option, missing):
    bad_file = tmp_path / "invalid.json"
    if not missing:
        bad_file.write_text("{broken JSON")
    args = ["convert", "--out", tmp_path / "draft"]
    if option != "--tree":
        args += ["--tree", sample_tree]
    code, stdout, stderr = cli(*args, option, bad_file)
    assert code != 0
    assert str(bad_file) in stdout + stderr
    assert ("cannot read" if missing else "invalid JSON") in stdout + stderr


def test_convert_reports_missing_frame_in_document_tree(cli, tmp_path, sample_tree):
    tree_path = tmp_path / "document.json"
    tree_path.write_text(json.dumps({"document": json.loads(sample_tree.read_text())}))
    code, stdout, stderr = cli("convert", "--tree", tree_path, "--frame", "missing", "--out", tmp_path / "draft")
    assert code != 0 and "frame missing not found" in stdout + stderr


@pytest.mark.parametrize("with_brand", [True, False], ids=["mapped", "unchanged-without-brand"])
def test_fonts_matches_registered_family_weight_and_italic(cli, tmp_path, monkeypatch, with_brand):
    styles = [{"fontFamily": "Display Serif VF", "fontWeight": 300},
              {"fontFamily": "Display Serif", "fontWeight": 200, "italic": True},
              {"fontFamily": "Body Serif VF", "fontWeight": 300, "italic": True}]
    tree = {"nodes": {"frame": {"document": {"type": "FRAME", "children": [
        {"type": "TEXT", "style": style} for style in styles]}}}}
    tree_path = tmp_path / "fonts.json"
    tree_path.write_text(json.dumps(tree))
    registered = ({("Display Serif", w, False) for w in (200, 300, 400, 600)}
                  | {("Body Serif", w, False) for w in range(200, 701, 100)}
                  | {("Body Serif", w, True) for w in (300, 400)}
                  | {("Body Sans", w, False) for w in (300, 400, 600)})
    monkeypatch.setattr(f2k, "fonts_registered", lambda company_id: registered)
    code, stdout, stderr = cli("fonts", "--tree", tree_path, "--company-id", "offline",
                               *(["--brand", BRAND] if with_brand else []))
    assert code == 0, stderr
    rows = {row["family"]: row for row in json.loads(stdout)}
    assert rows["Display Serif"]["klaviyo"] == "family registered, this weight/style missing"
    for family, mapped in (("Display Serif VF", "Display Serif"), ("Body Serif VF", "Body Serif")):
        assert rows[family]["klaviyo"] == ("registered" if with_brand else "NOT registered")
        if with_brand:
            assert rows[family]["klaviyo_family"] == mapped
        else:
            assert "klaviyo_family" not in rows[family]


# ---------------------------------------------------------------------------
# assets / upload --plan (all Loggie and download calls are fakes)
# ---------------------------------------------------------------------------

class FakeFigma:
    """Serves synthetic renders sized box * scale; records Klaviyo uploads."""

    def __init__(self, tree_path, missing=()):
        from assets import asset_plan
        self.plan = {e["node_id"]: e for e in asset_plan(build_ir(json.loads(Path(tree_path).read_text())))}
        self.missing = set(missing)
        self.calls = []
        self.uploads = []

    def loggie(self, slug, method, path, *, query=None, body=None, form=None):
        self.calls.append((slug, method, path, query, form))
        if path.startswith("/v1/images/"):
            assert "use_absolute_bounds=true" in query and "format=png" in query
            ids = query.split("ids=")[1].split("&")[0].split(",")
            return {"err": None, "images": {i: None if i in self.missing else f"fake://node/{i}" for i in ids}}
        if path.endswith("/images"):
            refs = {p["imageRef"] for e in self.plan.values() for p in e["paints"] if p.get("imageRef")}
            return {"meta": {"images": {r: f"fake://ref/{r}" for r in refs}}}
        if path == "/image-upload":
            name = next(f for f in form if f.startswith("name=")).split("=", 1)[1]
            self.uploads.append((name, next(f for f in form if f.startswith("hidden="))))
            n = len(self.uploads)
            return {"data": {"id": f"img{n}", "attributes": {"image_url": f"https://img.test/{n}/{name}"}}}
        raise AssertionError(path)

    def download(self, url, dest):
        from PIL import Image as PILImage
        kind, key = url.removeprefix("fake://").split("/", 1)
        if kind == "node":
            box = self.plan[key]["box"]
            img = PILImage.new("RGB", (round(box["w"] * 2), round(box["h"] * 2)), (10, 120, 200))
        else:
            img = PILImage.new("RGB", (300, 300), (200, 100, 50))
        img.save(dest, "PNG")


@pytest.fixture
def fake_figma(monkeypatch):
    def install(tree, missing=()):
        fake = FakeFigma(tree, missing)
        monkeypatch.setattr(f2k, "loggie", fake.loggie)
        monkeypatch.setattr(f2k, "download", fake.download)
        return fake
    return install


def assets_args(tree, out, *extra):
    return ["assets", "--tree", tree, "--figma-slug", "fig", "--file", "FILEKEY", "--out", out, *extra]


def test_upload_plan_idempotent_and_force(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan_path = out / "asset_plan.json"
    plan = json.loads(plan_path.read_text())
    code, _, stderr = cli("upload", "--plan", plan_path, "--slug", "kl", "--prefix", "run1-")
    assert code == 0, stderr
    assets = json.loads((out / "assets.json").read_text())
    assert set(assets) == {e["node_id"] for e in plan} and all({"id", "url", "sha256", "slug"} == set(v) for v in assets.values())
    assert [u[0] for u in fake.uploads] == ["run1-" + e["filename"] for e in plan]
    assert all(u[1] == "hidden=true" for u in fake.uploads)
    count = len(fake.uploads)
    assert cli("upload", "--plan", plan_path, "--slug", "kl")[0] == 0
    assert len(fake.uploads) == count
    assert cli("upload", "--plan", plan_path, "--slug", "kl", "--force")[0] == 0
    assert len(fake.uploads) == 2 * count
    assert json.loads((out / "assets.json").read_text())[plan[0]["node_id"]]["id"] != assets[plan[0]["node_id"]]["id"]


def test_upload_plan_refuses_missing_files(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan = json.loads((out / "asset_plan.json").read_text())
    (out / plan[-1]["filename"]).unlink()
    code, _, stderr = cli("upload", "--plan", out / "asset_plan.json", "--slug", "kl")
    assert code != 0 and plan[-1]["filename"] in stderr
    assert fake.uploads == [] and not (out / "assets.json").exists()


def test_upload_requires_plan_or_images(cli):
    code, _, stderr = cli("upload", "--slug", "kl")
    assert code != 0 and "--image" in stderr


def test_assets_unreadable_image_is_an_error_not_a_traceback(cli, tmp_path, fake_figma, monkeypatch, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    node_id = next(e["node_id"] for e in fake.plan.values() if e["export"] == "node")
    monkeypatch.setattr(f2k, "download", lambda url, dest: Path(dest).write_bytes(b"not an image"))
    code, stdout, _ = cli(*assets_args(tree, tmp_path / "assets", "--only", node_id))
    assert code != 0 and node_id in json.loads(stdout)["errors"]
    assert (tmp_path / "assets/asset_plan.json").is_file()


def test_upload_plan_reuploads_only_changed_files(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan_path = out / "asset_plan.json"
    plan = json.loads(plan_path.read_text())
    assert cli("upload", "--plan", plan_path, "--slug", "kl")[0] == 0
    before = json.loads((out / "assets.json").read_text())
    count = len(fake.uploads)
    changed = plan[0]
    with (out / changed["filename"]).open("ab") as handle:
        handle.write(b"\0")
    assert cli("upload", "--plan", plan_path, "--slug", "kl")[0] == 0
    assert len(fake.uploads) == count + 1 and fake.uploads[-1][0] == changed["filename"]
    after = json.loads((out / "assets.json").read_text())
    assert after[changed["node_id"]]["sha256"] != before[changed["node_id"]]["sha256"]
    assert {k: v for k, v in after.items() if k != changed["node_id"]} == \
        {k: v for k, v in before.items() if k != changed["node_id"]}


def test_upload_plan_reuploads_entries_without_hash(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan = json.loads((out / "asset_plan.json").read_text())
    (out / "assets.json").write_text(json.dumps(
        {e["node_id"]: {"id": "old", "url": "https://old"} for e in plan}))
    assert cli("upload", "--plan", out / "asset_plan.json", "--slug", "kl")[0] == 0
    assert len(fake.uploads) == len(plan)


def test_upload_plan_corrupt_assets_json_is_a_clear_error(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    (out / "assets.json").write_text("{not json")
    code, _, stderr = cli("upload", "--plan", out / "asset_plan.json", "--slug", "kl")
    assert code != 0 and "assets.json" in stderr and fake.uploads == []


def test_asset_urls_collect_img_src_and_style_backgrounds():
    doc = ('<img src="https://cdn.test/a.png"><IMG alt="x" src=\'https://cdn.test/b.png\'>'
           '<td style="background-image:url(&quot;https://cdn.test/c.jpg&quot;)"></td>'
           '<td style="background:url(https://cdn.test/a.png)"></td><img src="data:image/png;base64,AA">')
    assert f2k.asset_urls(doc) == ["https://cdn.test/a.png", "https://cdn.test/b.png", "https://cdn.test/c.jpg"]


def test_asset_urls_handle_quoted_spaces_protocol_relative_and_real_background_attributes():
    doc = ('<td style="background:url(\'https://a.test/c d.png\')"></td>'
           '<td style=\'background:url("https://a.test/e f.png")\'></td>'
           '<img src="//cdn.test/x.png">'
           '<td background="https://a.test/bg.png"></td>'
           '<td data-background="https://a.test/data.png"></td>'
           '<p>background=https://a.test/text.png</p>'
           '<a title="x background=https://a.test/title.png" href="https://a.test/link">l</a>')
    assert f2k.asset_urls(doc) == ["https://a.test/c d.png", "https://a.test/e f.png",
                                   "https://cdn.test/x.png", "https://a.test/bg.png"]


def test_wait_for_assets_has_a_hard_upper_bound_and_reports_every_url():
    clock = [0.0]
    timeouts = []

    def status(url, timeout=15):
        timeouts.append(timeout)
        clock[0] += 15
        return 0

    urls = [f"https://x.test/{i}.png" for i in range(12)]
    missing = f2k.wait_for_assets(urls, timeout=60, interval=2, workers=1, status=status,
                                  sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
    assert missing == urls
    assert clock[0] <= 60 + 15
    assert max(timeouts) <= 15 and len(timeouts) < 12


def test_wait_for_assets_polls_pending_urls_concurrently():
    import threading
    barrier = threading.Barrier(4, timeout=5)

    def status(url, timeout=15):
        barrier.wait()
        return 200

    assert f2k.wait_for_assets([f"https://x.test/{i}" for i in range(4)], workers=4, status=status) == []


def test_check_skips_the_asset_wait_without_chrome(cli, tmp_path, monkeypatch):
    doc = '<html><body><img src="https://cdn.test/a.png" alt="a"></body></html>'
    monkeypatch.setattr(f2k, "loggie", lambda *args, **kwargs: {"data": {"attributes": {"html": doc}}})
    monkeypatch.setattr(f2k, "chrome", lambda: None)
    monkeypatch.setattr(f2k, "wait_for_assets", lambda urls: pytest.fail("must not wait without Chrome"))
    code, _stdout, stderr = cli("check", "--slug", "offline", "--template-id", "t", "--out", tmp_path)
    assert code == 0, stderr
    report = json.loads((tmp_path / "check.json").read_text())
    assert report["assets_unavailable"] is None and "Chrome not found" in report["assets_note"]


def test_convert_rejects_a_non_numeric_gutter(cli, tmp_path, sample_tree):
    brand = tmp_path / "brand.json"
    brand.write_text(json.dumps({"gutter": "wide"}))
    tree = sample_tree
    code, _stdout, stderr = cli("convert", "--tree", tree, "--brand", brand, "--out", tmp_path / "o")
    assert code != 0 and "gutter" in stderr and "Traceback" not in stderr


def test_wait_for_assets_retries_then_reports_the_unavailable():
    calls, clock = [], [0.0]

    def status(url, timeout=15):
        calls.append(url)
        return 200 if url.endswith("late.png") and calls.count(url) >= 3 else 404

    def sleep(seconds):
        clock[0] += seconds

    missing = f2k.wait_for_assets(["https://x.test/late.png", "https://x.test/never.png"], timeout=10,
                                  interval=2, status=status, sleep=sleep, clock=lambda: clock[0])
    assert missing == ["https://x.test/never.png"]
    assert calls.count("https://x.test/late.png") == 3
    assert calls.count("https://x.test/never.png") > 3
    assert clock[0] <= 10


def test_check_waits_for_assets_injects_fonts_only_into_the_screenshot_copy(cli, tmp_path, monkeypatch):
    doc = '<html><head><title>t</title></head><body><img src="https://cdn.test/a.png" alt="a"><p>Hi</p></body></html>'
    shots = []
    seen = []
    monkeypatch.setattr(f2k, "loggie", lambda *args, **kwargs: {"data": {"attributes": {"html": doc}}})
    monkeypatch.setattr(f2k, "wait_for_assets", lambda urls: seen.append(urls) or ["https://cdn.test/a.png"])
    monkeypatch.setattr(f2k, "chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(f2k, "screenshot", lambda chrome_bin, target, png, width, height: (
        shots.append((target.name, height)), PilImage.new("RGB", (width, 50), "white").save(png)))
    code, _stdout, stderr = cli("check", "--slug", "offline", "--template-id", "t", "--out", tmp_path,
                                "--font-account", "AB12cd")
    assert code == 0, stderr
    assert seen == [["https://cdn.test/a.png"]]
    assert json.loads((tmp_path / "check.json").read_text())["assets_unavailable"] == ["https://cdn.test/a.png"]
    link = "https://static-forms.klaviyo.com/fonts/api/v1/AB12cd/custom_fonts.css"
    assert (tmp_path / "rendered.html").read_text() == doc
    assert link in (tmp_path / "rendered-screenshot.html").read_text()
    assert shots[0] == ("rendered-screenshot.html", 4000)
    assert 'src="rendered-screenshot.html"' in (tmp_path / "mobile-frame.html").read_text()


def test_screenshot_command_uses_virtual_time_budget_and_never_launches_chrome(monkeypatch):
    launched = []
    monkeypatch.setattr(f2k.subprocess, "run", lambda cmd, **kwargs: launched.append(cmd))
    f2k.screenshot("/fake/chrome", Path("/tmp/x.html"), Path("/tmp/x.png"), 600, 4000)
    assert "--virtual-time-budget=10000" in launched[0]
    assert "--window-size=600,4000" in launched[0]


def test_font_link_goes_into_head_and_survives_headless_documents():
    assert f2k.with_font_link("<html><head></head><body></body></html>", "Z").count("custom_fonts.css") == 1
    assert f2k.with_font_link("<p>x</p>", "Z").startswith("<head><link")


def _hidden_text_tree(tmp_path):
    def text(node_id, chars, **extra):
        return {"id": node_id, "type": "TEXT", "name": node_id, "characters": chars,
                "style": {"fontFamily": extra.pop("family", "Shown Sans"), "fontWeight": 400}, **extra}
    tree = {"nodes": {"frame": {"document": {"id": "frame", "type": "FRAME", "name": "f", "children": [
        text("shown", "Shown copy"),
        text("ghost", "Ghost copy", opacity=0, family="Ghost Serif"),
        {"id": "g", "type": "GROUP", "name": "g", "opacity": 0, "children": [
            text("nested", "Nested ghost", family="Nested Ghost")]}]}}}}
    path = tmp_path / "tree.json"
    path.write_text(json.dumps(tree))
    return path


def test_fonts_needed_skips_opacity_zero_text(tmp_path):
    assert f2k.fonts_needed(str(_hidden_text_tree(tmp_path))) == {("Shown Sans", 400, False)}


def test_check_text_walker_ignores_opacity_zero_text(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(f2k, "loggie", lambda *a, **k: {"data": {"attributes": {"html": "<p>Shown copy</p>"}}})
    monkeypatch.setattr(f2k, "chrome", lambda: None)
    code, stdout, stderr = cli("check", "--slug", "s", "--template-id", "t", "--out", tmp_path / "o",
                               "--tree", _hidden_text_tree(tmp_path))
    assert code == 0, stderr
    report = json.loads(stdout)
    assert report["figma_text_nodes"] == 1 and report["figma_text_missing_in_klaviyo"] == []


def test_outline_uses_the_converters_topmost_fill_and_skips_opacity_zero(cli, tmp_path):
    def solid(r):
        return {"type": "SOLID", "color": {"r": r, "g": 0, "b": 0}}
    box = {"x": 0, "y": 0, "width": 10, "height": 10}
    tree = {"nodes": {"frame": {"document": {"id": "frame", "type": "FRAME", "name": "f",
                                              "absoluteBoundingBox": box, "children": [
        {"id": "r", "type": "RECTANGLE", "name": "r", "absoluteBoundingBox": box,
         "fills": [solid(1), solid(0)]},
        {"id": "z", "type": "RECTANGLE", "name": "z", "absoluteBoundingBox": box, "opacity": 0}]}}}}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(tree))
    code, stdout, stderr = cli("outline", "--tree", path)
    assert code == 0, stderr
    assert "fill=#000000" in stdout and "fill=#FF0000" not in stdout
    assert next(line for line in stdout.splitlines() if "'z'" in line).endswith("[HIDDEN, skip]")


def test_font_css_url_is_quoted_once():
    assert f2k.font_css_url("a/b c") == "https://static-forms.klaviyo.com/fonts/api/v1/a%2Fb%20c/custom_fonts.css"
    assert f2k.font_css_url("Z") in f2k.with_font_link("<p>x</p>", "Z")


def test_assets_overrides_skip_is_not_planned_or_exported(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake_figma(tree)
    full = tmp_path / "full"
    assert cli(*assets_args(tree, full))[0] == 0
    ids = [e["node_id"] for e in json.loads((full / "asset_plan.json").read_text())]
    skipped = ids[-1]
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"skip": [skipped]}))
    out = tmp_path / "skipped"
    code, stdout, stderr = cli(*assets_args(tree, out, "--overrides", overrides))
    assert code == 0, stderr
    planned = [e["node_id"] for e in json.loads((out / "asset_plan.json").read_text())]
    assert planned == ids[:-1] and skipped not in json.loads(stdout)["written"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"skip": "5:3"}))
    code, _, stderr = cli(*assets_args(tree, tmp_path / "x", "--overrides", bad))
    assert code != 0 and "skip must be a list" in stderr


def test_upload_plan_reuploads_when_the_slug_changes_or_is_missing(cli, tmp_path, fake_figma, sample_tree):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan_path = out / "asset_plan.json"
    count = len(json.loads(plan_path.read_text()))
    assert cli("upload", "--plan", plan_path, "--slug", "sandbox")[0] == 0
    assert {v["slug"] for v in json.loads((out / "assets.json").read_text()).values()} == {"sandbox"}
    assert len(fake.uploads) == count
    assert cli("upload", "--plan", plan_path, "--slug", "production")[0] == 0
    assert len(fake.uploads) == 2 * count
    record = json.loads((out / "assets.json").read_text())
    assert {v["slug"] for v in record.values()} == {"production"} and len(record) == count
    assert cli("upload", "--plan", plan_path, "--slug", "production")[0] == 0
    assert len(fake.uploads) == 2 * count
    for entry in record.values():  # older files without a slug are not trusted
        del entry["slug"]
    (out / "assets.json").write_text(json.dumps(record))
    assert cli("upload", "--plan", plan_path, "--slug", "production")[0] == 0
    assert len(fake.uploads) == 3 * count


@pytest.mark.parametrize("filename", ["../evil.jpg", "sub/x.jpg", "/etc/passwd", "a b.jpg", "..", ""])
def test_upload_plan_rejects_unsafe_filenames(cli, tmp_path, fake_figma, sample_tree, filename):
    tree = sample_tree
    fake = fake_figma(tree)
    out = tmp_path / "assets"
    assert cli(*assets_args(tree, out))[0] == 0
    plan_path = out / "asset_plan.json"
    plan = json.loads(plan_path.read_text())
    plan[0]["filename"] = filename
    plan_path.write_text(json.dumps(plan))
    (tmp_path / "evil.jpg").write_bytes(b"x")
    code, _, stderr = cli("upload", "--plan", plan_path, "--slug", "kl")
    assert code != 0 and "unsafe filename" in stderr and fake.uploads == []


def _export_tree(tmp_path):
    def n(node_id, node_type, **extra):
        return {"id": node_id, "name": node_id, "type": node_type, **extra}
    tree = {"nodes": {"1:0": {"document": n("1:0", "FRAME", children=[
        n("1:1", "RECTANGLE"),
        n("1:2", "GROUP", children=[n("1:3", "VECTOR"), n("1:4", "TEXT", characters="live")]),
        n("1:5", "GROUP", children=[n("1:6", "VECTOR"), n("1:7", "TEXT", characters="gone", visible=False)])])}}}
    path = tmp_path / "tree.json"
    path.write_text(json.dumps(tree))
    return path


@pytest.fixture
def export_fakes(monkeypatch):
    calls = []

    def fake_loggie(slug, method, path, *, query=None, **kwargs):
        calls.append(query)
        ids = query.split("ids=")[1].split("&")[0].split(",")
        return {"err": None, "images": {i: f"fake://{i}" for i in ids}}

    def fake_download(url, dest):
        PilImage.new("RGB", (4, 4)).save(dest, "PNG")

    monkeypatch.setattr(f2k, "loggie", fake_loggie)
    monkeypatch.setattr(f2k, "download", fake_download)
    return calls


def export_args(tree, out, *nodes, extra=()):
    return ["export", "--tree", tree, "--figma-slug", "fig", "--file", "K", "--out", out,
            *[arg for node in nodes for arg in ("--node", node)], *extra]


def test_export_refuses_nodes_with_live_text_and_names_the_node(cli, tmp_path, export_fakes):
    tree = _export_tree(tmp_path)
    code, _, stderr = cli(*export_args(tree, tmp_path / "o", "1:1=ok", "1:2=bad"))
    assert code != 0 and "1:2" in stderr and "live text" in stderr and "1:4" in stderr
    assert export_fakes == [] and not (tmp_path / "o" / "ok.png").exists()  # nothing exported at all
    code, _, stderr = cli(*export_args(tree, tmp_path / "o", "1:0=frame"))
    assert code != 0 and "live text" in stderr


def test_export_allows_text_free_nodes_and_hidden_text(cli, tmp_path, export_fakes):
    code, stdout, stderr = cli(*export_args(_export_tree(tmp_path), tmp_path / "o", "1:1=rect", "1:5=art"))
    assert code == 0, stderr
    assert json.loads(stdout) == {"rect": "rect.png", "art": "art.png"}
    assert (tmp_path / "o" / "rect.png").is_file()


@pytest.mark.parametrize("name", ["../../escaped", "/tmp/abs", "a/b\\c", ".."])
def test_export_sanitizes_names_and_stays_inside_out(cli, tmp_path, export_fakes, name):
    out = tmp_path / "deep" / "o"
    code, stdout, _ = cli(*export_args(_export_tree(tmp_path), out, f"1:1={name}"))
    written = json.loads(stdout) if code == 0 else {}
    if code == 0:
        ((_, filename),) = written.items()
        assert "/" not in filename and "\\" not in filename and not filename.startswith(".")
        assert (out / filename).is_file()
    assert [p for p in tmp_path.rglob("*.png")] == ([out / filename] if code == 0 else [])
    assert not (tmp_path / "escaped.png").exists() and not (tmp_path / "deep" / "escaped.png").exists()


def test_export_rejects_malformed_node_and_unknown_node_and_needs_tree(cli, tmp_path, export_fakes):
    tree = _export_tree(tmp_path)
    for bad in ("1:1", "=name", "1:1="):
        code, _, stderr = cli(*export_args(tree, tmp_path / "o", bad))
        assert code != 0 and "ID=NAME" in stderr
    code, _, stderr = cli(*export_args(tree, tmp_path / "o", "9:9=x"))
    assert code != 0 and "9:9" in stderr
    code, _, stderr = cli("export", "--figma-slug", "f", "--file", "K", "--out", tmp_path, "--node", "1:1=x")
    assert code != 0 and "--tree" in stderr
    assert export_fakes == []


@pytest.mark.parametrize("command", ["validate", "push"])
def test_validate_and_push_report_unreadable_bodies_without_a_traceback(cli, tmp_path, command):
    extra = ["--slug", "kl"] if command == "push" else []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    for path, needle in ((tmp_path / "missing.json", "cannot read"), (bad, "invalid JSON")):
        code, _, stderr = cli(command, "--body", path, *extra)
        assert code != 0 and needle in stderr and "Traceback" not in stderr
    bad.write_text("[]")
    code, _, stderr = cli(command, "--body", bad, *extra)
    assert code != 0 and "must contain a JSON object" in stderr


@pytest.mark.parametrize("command", ["fonts", "outline"])
def test_tree_readers_report_bad_trees(cli, tmp_path, command):
    for content, needle in ((None, "cannot read"), ("{nope", "invalid JSON"), ("{}", "no 'nodes' map")):
        path = tmp_path / "tree.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(content)
        code, _, stderr = cli(command, "--tree", path)
        assert code != 0 and needle in stderr and "Traceback" not in stderr


def test_check_reads_tree_and_context_before_any_network_call(cli, tmp_path, loggie_calls):
    common = ["check", "--slug", "s", "--template-id", "t", "--out", tmp_path / "o"]
    code, _, stderr = cli(*common, "--tree", tmp_path / "missing.json")
    assert code != 0 and "cannot read tree" in stderr
    code, _, stderr = cli(*common, "--context", "{nope")
    assert code != 0 and "--context" in stderr
    code, _, stderr = cli(*common, "--context", "[1]")
    assert code != 0 and "--context must be a JSON object" in stderr
    assert loggie_calls == []


def test_loggie_reports_a_missing_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("loggie")
    monkeypatch.undo()  # the autouse fixture replaced f2k.loggie; use the real one
    monkeypatch.setattr(f2k.subprocess, "run", missing)
    with pytest.raises(SystemExit) as exc:
        f2k.loggie("s", "GET", "/x")
    assert "loggie not found" in str(exc.value)


def test_upload_image_requires_name_equals_path(cli, tmp_path, loggie_calls):
    code, _, stderr = cli("upload", "--slug", "kl", "--out", tmp_path, "--image", "just-a-path.png")
    assert code != 0 and "NAME=PATH" in stderr and loggie_calls == []


@pytest.mark.parametrize("overrides, needle", [
    ({"skip": "5:3"}, "skip must be a list"),
    ({"skip": [1]}, "skip must be a list"),
    ({"links": []}, "links must be an object"),
    ({"alt": {"1:1": 5}}, "alt must be an object"),
    ({"text": "hello"}, "text must be an object"),
    ({"roles": {"1:1": "h7"}}, "roles values must be one of"),
    ({"roles": {"1:1": ["h1"]}}, "roles must be an object"),
])
def test_convert_rejects_malformed_override_shapes(cli, tmp_path, sample_tree, overrides, needle):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(overrides))
    tree = sample_tree
    code, _, stderr = cli("convert", "--tree", tree, "--overrides", path, "--out", tmp_path / "o")
    assert code != 0 and needle in stderr and "Traceback" not in stderr
    assert not (tmp_path / "o" / "template_body.json").exists()
