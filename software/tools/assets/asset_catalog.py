"""Make the central catalog API available to standalone authoring scripts."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from robbi.asset_catalog import catalog_group, catalog_path, read_catalog

__all__ = ["catalog_group", "catalog_path", "read_catalog"]
