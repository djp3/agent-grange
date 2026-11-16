#!/usr/bin/env python3
"""Launch-time freshness check, then exec the real command.

Decision logic lives in freshness.py and is shared with server.py, which
repeats the check while running. This file only decides start vs refuse:

  FRESH                         start
  AHEAD                         start, with a warning (unreleased or unpushed work on top of the tag)
  STALE                         REFUSE (exit 78): tag is newer than, or diverged from, this image
  UNVERIFIABLE                  REFUSE if FRESHNESS_STRICT=1 (compose default), else warn and start
  NO_METADATA                   REFUSE: image was built without the Makefile
"""
import os
import sys

import freshness

EX_CONFIG = 78


def log(msg: str) -> None:
    print(f"freshness check: {msg}", file=sys.stderr, flush=True)


def refuse(lines: list[str]) -> None:
    bar = "=" * 69
    print(f"\n{bar}\n REFUSING TO START: this image is out of date.\n", file=sys.stderr)
    for line in lines:
        print(f"   {line}", file=sys.stderr)
    print(f"\n Fix:       {freshness.FIX}"
          f"\n Bypass:    SKIP_FRESHNESS_CHECK=1 make up\n{bar}\n", file=sys.stderr, flush=True)
    sys.exit(EX_CONFIG)


def main() -> None:
    cfg = freshness.Config.from_env()
    if cfg.skip:
        log("skipped (SKIP_FRESHNESS_CHECK=1)")
    else:
        if cfg.dirty:
            log(f"warning: built from a dirty working tree, so the image may not match commit {cfg.built[:12]}")
        res = freshness.assess(cfg)
        if res.blocks_start(cfg.strict):
            if res.verdict is freshness.Verdict.UNVERIFIABLE:
                refuse([f"cannot verify freshness: {res.summary}",
                        "FRESHNESS_STRICT=1, so refusing rather than guessing"])
            refuse([res.summary] + res.details if res.verdict is freshness.Verdict.NO_METADATA else res.details)
        log(("ok" if res.verdict is freshness.Verdict.FRESH else "warning") + f": {res.summary}")

    if len(sys.argv) < 2:
        sys.exit("entrypoint: no command given")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
