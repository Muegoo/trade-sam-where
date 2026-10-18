"""Paper mode must not import the optional, Windows-only MT5 adapter."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class PaperPortabilityTests(unittest.TestCase):
    def test_research_analysis_imports_without_metatrader(self):
        path = Path(__file__).resolve().parents[1] / "gold_demo_alert.py"
        spec = importlib.util.spec_from_file_location("paper_research_probe", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"MetaTrader5": None}):
            spec.loader.exec_module(module)
        self.assertEqual(module.classify(.7), "WATCH_BUY")
        self.assertEqual(module.classify(.5), "WAIT")


if __name__ == "__main__":
    unittest.main()
