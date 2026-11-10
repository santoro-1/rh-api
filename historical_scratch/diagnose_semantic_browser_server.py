from __future__ import annotations

import faulthandler
from pathlib import Path
import sys


diagnostic_path = Path(
    r"D:\工作内容\轻盈健\数字人\.codex_tmp\browser_acceptance_storage\startup_stack.log"
)
diagnostic_file = diagnostic_path.open("w", encoding="utf-8")
faulthandler.dump_traceback_later(5, repeat=False, file=diagnostic_file)

sys.path.insert(0, str(Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")))

from apps.processor.run_web_api import main  # noqa: E402


raise SystemExit(main(["--host", "127.0.0.1", "--port", "8001"]))
