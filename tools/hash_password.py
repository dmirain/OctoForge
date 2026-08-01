#!/usr/bin/env python
"""Print an `OF_ADMIN_PASSWORD_HASH` value for the operator credential.

    python tools/hash_password.py                 # generate a password too
    python tools/hash_password.py --password ...  # hash one you chose

The generated password is shown once and never stored: only the hash belongs in
`.env`.
"""

import argparse
import secrets

from octoforge_server.auth import hash_password

GENERATED_BYTES = 18  # 24 characters of base64url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", help="password to hash; generated when omitted")
    args = parser.parse_args()

    password = args.password or secrets.token_urlsafe(GENERATED_BYTES)
    if not args.password:
        print(f"password: {password}")
    print(f"OF_ADMIN_PASSWORD_HASH={hash_password(password)}")


if __name__ == "__main__":
    main()
