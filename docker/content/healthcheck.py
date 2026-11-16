#!/usr/bin/env python3
"""Docker HEALTHCHECK command.

Exit 0 if /health answers 200. Otherwise print a one-line reason and exit 1.
Docker keeps that line in the container's health log, where `docker inspect`
and the host-side query_server.py can read it.
"""
import json
import os
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/health"
try:
    with urllib.request.urlopen(url, timeout=2):
        print("ok")
except urllib.error.HTTPError as e:
    try:
        body = json.load(e)
        print(f"HTTP {e.code}: {body.get('summary') or body.get('error')}. Fix: {body.get('fix', '')}")
    except Exception:
        print(f"HTTP {e.code} {e.reason}")
    sys.exit(1)
except Exception as e:
    print(f"unreachable: {e}")
    sys.exit(1)
