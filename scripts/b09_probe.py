#!/usr/bin/env python3
"""Print a capability report without changing worker readiness."""

import json
import sys

from nexa.worker.capabilities import inventory_to_json
from nexa.worker.errors import WorkerError
from nexa.worker.probes import live_provider


def main() -> int:
    try:
        provider = live_provider()
        inventory = provider.discover()
        snapshot = provider.allocatable(inventory)
        print(json.dumps(inventory_to_json(inventory, snapshot), sort_keys=True))
        return 0
    except WorkerError as exc:
        print(
            json.dumps({"status": "blocked", "code": exc.code, "reason": str(exc)}, sort_keys=True)
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
