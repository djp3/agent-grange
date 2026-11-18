"""The image carries the server modules and the grange package (plan phase 0)."""
import importlib
import importlib.util
import unittest


class LayoutTest(unittest.TestCase):
    def test_grange_package_imports(self):
        importlib.import_module("grange")

    def test_freshness_imports(self):
        importlib.import_module("freshness")

    def test_entry_scripts_are_present(self):
        # Present, not imported: healthcheck.py makes a request and entrypoint.py
        # execs a command as soon as they run.
        for name in ("entrypoint", "healthcheck", "server"):
            self.assertIsNotNone(importlib.util.find_spec(name), name)


if __name__ == "__main__":
    unittest.main()
