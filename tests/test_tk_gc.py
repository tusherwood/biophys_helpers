"""tk_gc: nested pause/resume keeps automatic GC off until the last resume."""
import gc
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tk_gc  # noqa: E402


class TkGcTests(unittest.TestCase):
    def tearDown(self):
        while tk_gc._depth:
            tk_gc.resume()
        gc.enable()

    def test_nested_pauses(self):
        self.assertTrue(gc.isenabled())
        tk_gc.pause()
        tk_gc.pause()
        self.assertFalse(gc.isenabled())
        tk_gc.tick()
        tk_gc.resume()
        self.assertFalse(gc.isenabled())
        tk_gc.resume()
        self.assertTrue(gc.isenabled())
        tk_gc.resume()                    # extra resume is a no-op
        self.assertTrue(gc.isenabled())


if __name__ == '__main__':
    unittest.main()
