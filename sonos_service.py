#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys

from omasonos_backend.controller import SonosController
from omasonos_backend.protocol import ProtocolServer


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ProtocolServer(SonosController()).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
