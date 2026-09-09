from __future__ import annotations

import argparse
from pathlib import Path

from .index import build_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the reviewed read-only dek wiki index")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_index(args.vault, args.output)
    print(f'documents={len(result["documents"])}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
