# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Generate local private access material, never print it or upload it."""

import json
import os
import secrets
from pathlib import Path

from app.access import make_verifier
from app.config import ROOT


def generate(directory: Path) -> None:
    directory.mkdir(mode=0o700, exist_ok=False)
    passphrase = secrets.token_urlsafe(24)
    verifier = make_verifier(passphrase)
    contents = {
        "access.json": json.dumps({"passphrase": passphrase, "session_expiry_seconds": 3600}) + "\n",
        "verifier": verifier + "\n",
    }
    for name, content in contents.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(content)


def main():
    target = ROOT / ".private-demo"
    try:
        generate(target)
    except FileExistsError:
        raise SystemExit("Private access directory already exists; nothing overwritten. Review it locally.")
    print(f"Private access material created in {target} (directory 700; files 600).")
    print("Open access.json in a local editor to retrieve the passphrase. Do not paste it into chat.")
    print("The verifier file is backend-only. No secrets were uploaded or printed.")


if __name__ == "__main__":
    main()
