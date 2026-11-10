from __future__ import annotations

import secrets
import sys
from pathlib import Path


project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

password_file = Path("/opt/runninghub-video/data/initial-admin-password.txt")
if password_file.exists():
    raise SystemExit(f"{password_file} already exists; refusing to overwrite")

password = secrets.token_urlsafe(24)
sys.argv = ["create_admin", "admin", "--password", password]

from scripts.create_admin import main


main()
password_file.write_text(password + "\n", encoding="utf-8")
password_file.chmod(0o600)
