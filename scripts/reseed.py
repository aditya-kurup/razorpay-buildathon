import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from merchant.db import reset_and_seed

reset_and_seed()
print("Reset and seeded merchant.sqlite3")
