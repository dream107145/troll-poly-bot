#!/usr/bin/env python
"""Print a TPB_ADMIN_PASSWORD_HASH line for .env.

Storing the hash instead of the password means a leaked .env does not hand
over a usable credential -- only something a GPU has to chew through.

    python scripts/hash_password.py                 # prompts, nothing echoed
    python scripts/hash_password.py --check         # verify one against a hash

The password is read with getpass rather than taken as an argument, because an
argument lands in the shell history and in every `ps` listing on the box.
"""
from __future__ import annotations

import argparse
import getpass
import sys

sys.path.insert(0, "src")

from troll_poly_bot.web.auth import hash_password, verify_password  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", metavar="HASH",
                    help="verify a password against an existing hash")
    args = ap.parse_args()

    password = getpass.getpass("password: ")
    if not password:
        raise SystemExit("empty password")

    if args.check:
        ok = verify_password(password, args.check)
        print("matches" if ok else "does NOT match")
        raise SystemExit(0 if ok else 1)

    if len(password) < 8:
        raise SystemExit("too short: use at least 8 characters")
    if password != getpass.getpass("again: "):
        raise SystemExit("the two entries differ")

    print("\nAdd this to .env (and remove TPB_ADMIN_PASSWORD):\n")
    print(f"TPB_ADMIN_PASSWORD_HASH={hash_password(password)}")


if __name__ == "__main__":
    main()
