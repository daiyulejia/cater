"""Fixture readiness means the complete port value has been published."""
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class AcpFixtureTests(unittest.TestCase):
    def test_port_marker_is_not_visible_during_content_write(self):
        fixture = runpy.run_path(str(Path(__file__).parent / "fixtures" / "acp_peer.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write = Path.write_text
            stages = []

            def observed_write(path, content, *args, **kwargs):
                self.assertFalse((root / "child-port").exists())
                stages.append(path.name)
                written = write(path, content, *args, **kwargs)
                self.assertFalse((root / "child-port").exists())
                return written

            with patch.object(Path, "write_text", autospec=True, side_effect=observed_write):
                fixture["publish_port"](root, 23456)
            self.assertEqual(stages, ["child-port.pending"])
            self.assertEqual((root / "child-port").read_text(), "23456")
            self.assertFalse((root / "child-port.pending").exists())
