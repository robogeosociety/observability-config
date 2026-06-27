"""Make the collector scripts importable as top-level modules in the tests."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
