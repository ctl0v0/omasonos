import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "manifest.json").read_text())

assert manifest["schemaVersion"] == 1
for field in ("id", "name", "version", "kinds", "entryPoints"):
    assert field in manifest and manifest[field]
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", manifest["id"])
assert ".." not in manifest["id"]
assert not manifest["id"].startswith("omarchy.")
assert manifest["barWidget"]["defaultSection"] in {"left", "center", "right"}
required = {
    "service": "service",
    "bar-widget": "barWidget",
    "panel": "panel",
    "menu": "menu",
    "overlay": "overlay",
    "bar": "bar",
}
for kind in manifest["kinds"]:
    key = required.get(kind)
    if key:
        path = manifest["entryPoints"][key]
        assert not path.startswith("/") and ".." not in path
        assert (ROOT / path).is_file()
