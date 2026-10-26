"""Run the fake InstaBox API on a fixed port for a manual end-to-end check.

    python tests/serve_mock.py --port 8787 --root /tmp/instabox-vm

Every request is appended to ``<root>/requests.jsonl`` so the caller can prove which calls the
Hermes terminal backend actually made.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from mock_api import MockInstabox


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", default="/tmp/instabox-vm")
    args = parser.parse_args()

    log = Path(args.root) / "requests.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    server = MockInstabox(Path(args.root), port=args.port).start()
    print(f"mock InstaBox listening on {server.url} (log: {log})", flush=True)
    try:
        while True:
            time.sleep(0.25)
            if server.requests:
                with log.open("a", encoding="utf-8") as handle:
                    for record in server.requests:
                        body = record["body"].decode("utf-8", "replace")
                        handle.write(json.dumps({**record, "body": body}) + "\n")
                server.requests.clear()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
