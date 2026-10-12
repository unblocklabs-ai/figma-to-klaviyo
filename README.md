# Figma to Klaviyo

Convert an approved Figma email frame into an editable Klaviyo drag-and-drop
template. Text and buttons stay live; images are reserved for photography,
illustration, logos, and icons.

## Use the skill

Start with [SKILL.md](SKILL.md) for the conversion workflow, review gates, and
handoff checklist. The skill can be installed with:

```sh
npx skills add unblocklabs-ai/figma-to-klaviyo
```

The agent workflow also uses the `klaviyo`, `email-design-figma`, and brand
skills from your environment; those are not bundled here.

## Run the converter

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Script dependencies
are declared inline and installed automatically by uv.

```sh
uv run scripts/f2k.py --help
uv run scripts/f2k.py convert \
  --tree path/to/node_tree.json \
  --brand path/to/brand.json \
  --out runs/my-email/
```

`convert`, `outline`, and `validate` work offline. Figma and Klaviyo API
commands require the `loggie` CLI with configured integration slugs.
Screenshot checks also require Chrome. Conversion drafts contain TODOs for
missing links, alt text, and uploaded assets; `push` refuses incomplete drafts.

See [configuration](references/config.md) and
[mapping rules](references/mapping-rules.md) for supported layouts and limits.
Keep credentials and live run outputs outside version control.

## Tests

```sh
uv run --python 3.11 --with pytest --with jsonschema --with referencing \
  --with pillow python -m pytest tests -q
```

Tests use synthetic inputs; client designs and assets are not included.
The vendored Klaviyo schema retains its
[MIT license notice](scripts/schemas/LICENSE-klaviyo-openapi.txt).
