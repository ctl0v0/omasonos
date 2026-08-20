# Contributing

Bug reports and focused pull requests are welcome. For sensitive reports, use
the process in [SECURITY.md](SECURITY.md).

## Development setup

OmaSonos targets Python 3.14 on Omarchy Quattro. Keep the development environment
outside the plugin tree because Omarchy correctly rejects plugin directories that
contain symlinks:

```bash
DEV_VENV="${XDG_CACHE_HOME:-$HOME/.cache}/omasonos-dev/venv"
python3 -m venv "$DEV_VENV"
"$DEV_VENV/bin/python" -m pip install pip==24.3.1
"$DEV_VENV/bin/python" -m pip install --require-hashes -r requirements.lock
"$DEV_VENV/bin/python" -m pip install pytest==9.1.1 pip-tools==7.5.0
```

Run the automated and Omarchy-hosted checks:

```bash
"$DEV_VENV/bin/python" -m pytest -q
"$DEV_VENV/bin/python" -m compileall -q sonos_service.py omasonos_backend scripts/smoke-backend.py
bash -n sonos-backend scripts/*.sh
./scripts/validate-plugin.sh
./scripts/lint-qml.sh
```

The final two commands require Omarchy. The validation script checks a clean staged
copy with Omarchy's authoritative validator, so ignored development files cannot
leak into local installs or releases. Hardware-affecting tests should use a test
household and must document the speaker model, firmware, and topology tested.

## Updating dependencies

Edit `requirements.in`, then regenerate the lock on Python 3.14:

```bash
"$DEV_VENV/bin/pip-compile" \
  --generate-hashes \
  --resolver=backtracking \
  --strip-extras \
  --output-file=requirements.lock \
  requirements.in
```

Commit both dependency files together. Do not remove hashes or loosen direct
dependency pins without explaining why in the pull request.
