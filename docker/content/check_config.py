"""The configuration check of startup step 2, run before the sidecar starts.

Compose runs this as the one-shot `check` service, and the tailscale sidecar
starts only once it has exited 0 (R8.1). The sidecar's hostname is
grange-<GRANGE_ID>, so without this an id the grange would refuse, such as one
with a capital letter, reaches the control server as a device name before the
grange has looked at it.

Same check, same message, and same exit code as server.py. The id is also
checked exactly as written, because compose hands the sidecar the value
unstripped.
"""
from __future__ import annotations

import os
import sys

from grange.config import EX_CONFIG, Config, ConfigError, check_grange_id


def main() -> None:
    try:
        cfg = Config.from_env()
        check_grange_id(os.environ.get("GRANGE_ID", ""))
    except ConfigError as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr, flush=True)
        sys.exit(EX_CONFIG)
    print(f"configuration ok: GRANGE_ID={cfg.grange_id}", flush=True)


if __name__ == "__main__":
    main()
