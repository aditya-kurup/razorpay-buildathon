#!/usr/bin/env python3
"""Verify the merchant-service audit_events hash chain."""

from __future__ import annotations

import argparse
from pathlib import Path

from merchant.audit_events import verify_audit_chain
from merchant.db import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    result = verify_audit_chain(args.db)
    print(result)
    return 0 if result == "chain intact" else 1


if __name__ == "__main__":
    raise SystemExit(main())
