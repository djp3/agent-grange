"""Configuration (section 7, startup step 2)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import grange
from grange.config import EX_CONFIG, Config, ConfigError, parse_peers

CHECK = Path(grange.__file__).resolve().parent.parent / "check_config.py"


class DefaultsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.from_env({"GRANGE_ID": "djp3"})
        self.assertEqual(cfg.grange_id, "djp3")
        self.assertEqual((cfg.port, cfg.gossip_port, cfg.group, cfg.ts_tag), (7788, 7789, "default", "tag:grange"))
        self.assertEqual(cfg.peers, ())
        self.assertFalse(cfg.plain_mode)
        self.assertEqual((cfg.heartbeat_s, cfg.farm_stale_s, cfg.expire_s, cfg.forget_s),
                         (10.0, 60.0, 60.0, 600.0))
        self.assertEqual((cfg.repair_s, cfg.discovery_s, cfg.fanout), (3.0, 30.0, 5))
        self.assertEqual((cfg.connect_timeout_s, cfg.exchange_timeout_s), (1.0, 3.0))

    def test_empty_value_means_default(self):
        env = {k: "" for k in ("GRANGE_PORT", "GRANGE_GOSSIP_PORT", "GRANGE_GROUP", "TS_TAG", "GRANGE_PEERS",
                               "HEARTBEAT_SECONDS", "FARM_STALE_SECONDS", "EXPIRE_SECONDS", "FORGET_SECONDS",
                               "REPAIR_SECONDS", "DISCOVERY_SECONDS", "FANOUT", "CONNECT_TIMEOUT_SECONDS",
                               "EXCHANGE_TIMEOUT_SECONDS")}
        env["GRANGE_ID"] = "djp3"
        self.assertEqual(Config.from_env(env), Config.from_env({"GRANGE_ID": "djp3"}))

    def test_values_are_read(self):
        cfg = Config.from_env({"GRANGE_ID": "alice", "GRANGE_PORT": "8000", "FANOUT": "2",
                               "EXPIRE_SECONDS": "90", "REPAIR_SECONDS": "0.5", "GRANGE_GROUP": "proto-work"})
        self.assertEqual((cfg.port, cfg.fanout, cfg.expire_s, cfg.repair_s, cfg.group),
                         (8000, 2, 90.0, 0.5, "proto-work"))

    def test_peers_make_plain_mode(self):
        cfg = Config.from_env({"GRANGE_ID": "grange-a", "GRANGE_PEERS": "grange-b:7789, grange-c:7789\ngrange-d:7789"})
        self.assertEqual(cfg.peers, ("grange-b:7789", "grange-c:7789", "grange-d:7789"))
        self.assertTrue(cfg.plain_mode)

    def test_peer_parsing(self):
        self.assertEqual(parse_peers("a:1 a:1 b:2"), ("a:1", "b:2"))
        for bad in ("a", "a:", ":80", "a:x", "a:0", "a:70000"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_peers(bad)


class GrangeIdTest(unittest.TestCase):
    def test_missing_refuses(self):
        for env in ({}, {"GRANGE_ID": ""}, {"GRANGE_ID": "   "}):
            with self.assertRaises(ConfigError) as cm:
                Config.from_env(env)
            self.assertIn("GRANGE_ID", str(cm.exception))

    def test_valid_ids(self):
        for gid in ("a", "djp3", "a-b", "0", "x" * 56, "a" + "-" * 54 + "b"):
            self.assertEqual(Config.from_env({"GRANGE_ID": gid}).grange_id, gid)

    def test_invalid_ids_refuse(self):
        for gid in ("-a", "a-", "-", "Djp3", "a_b", "a.b", "a b", "x" * 57, "é"):
            with self.assertRaises(ConfigError, msg=gid):
                Config.from_env({"GRANGE_ID": gid})


class BadValuesTest(unittest.TestCase):
    def test_bad_numbers_refuse(self):
        for key, value in (("GRANGE_PORT", "x"), ("GRANGE_PORT", "0"), ("GRANGE_GOSSIP_PORT", "65536"),
                           ("FANOUT", "0"), ("FANOUT", "2.5"), ("EXPIRE_SECONDS", "-1"),
                           ("EXPIRE_SECONDS", "0"), ("REPAIR_SECONDS", "nan"), ("HEARTBEAT_SECONDS", "inf"),
                           ("DISCOVERY_SECONDS", "soon")):
            with self.assertRaises(ConfigError, msg=f"{key}={value}"):
                Config.from_env({"GRANGE_ID": "djp3", key: value})


class CheckScriptTest(unittest.TestCase):
    """check_config.py, which compose runs before the sidecar starts (R8.1)."""

    def run_check(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in ("GRANGE_ID", "FANOUT")}
        return subprocess.run([sys.executable, str(CHECK)], env={**clean, **env},
                              capture_output=True, text=True, timeout=10)

    def test_valid_id_passes(self):
        done = self.run_check(GRANGE_ID="carol")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "configuration ok: GRANGE_ID=carol"))

    def test_capital_letter_refuses_with_the_reason(self):
        done = self.run_check(GRANGE_ID="Carol")
        self.assertEqual(done.returncode, EX_CONFIG)
        self.assertIn("REFUSING TO START: GRANGE_ID='Carol' is invalid", done.stderr)

    def test_missing_id_refuses(self):
        done = self.run_check()
        self.assertEqual(done.returncode, EX_CONFIG)
        self.assertIn("GRANGE_ID is not set", done.stderr)

    def test_spaces_around_the_id_refuse(self):
        """The grange strips them, but the sidecar's hostname would carry them."""
        done = self.run_check(GRANGE_ID=" carol")
        self.assertEqual(done.returncode, EX_CONFIG)
        self.assertIn("GRANGE_ID=' carol' is invalid", done.stderr)

    def test_other_keys_are_checked_too(self):
        done = self.run_check(GRANGE_ID="carol", FANOUT="0")
        self.assertEqual(done.returncode, EX_CONFIG)
        self.assertIn("FANOUT=0", done.stderr)


if __name__ == "__main__":
    unittest.main()
