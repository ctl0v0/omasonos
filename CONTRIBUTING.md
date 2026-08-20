# Contributing

Bug reports and focused pull requests are welcome. For sensitive reports, use
the process in [SECURITY.md](SECURITY.md).

## Development setup

OmaSonos targets Python 3.14 on Omarchy Quattro. Create an isolated environment
and install the runtime and test dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pip==24.3.1
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install pytest==9.1.1 pip-tools==7.5.0
```

Run the checks used by CI:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q sonos_service.py omasonos_backend scripts/smoke-backend.py
bash -n sonos-backend scripts/install-local.sh scripts/test-local.sh
.venv/bin/python tests/validate_manifest.py
omarchy plugin validate .
```

The final command requires Omarchy. Hardware-affecting tests should use a test
household and must document the speaker model, firmware, and topology tested.

## Updating dependencies

Edit `requirements.in`, then regenerate the lock on Python 3.14:

```bash
.venv/bin/pip-compile \
  --generate-hashes \
  --resolver=backtracking \
  --strip-extras \
  --output-file=requirements.lock \
  requirements.in
```

Commit both dependency files together. Do not remove hashes or loosen direct
dependency pins without explaining why in the pull request.
