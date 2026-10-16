"""Tests check the German texts (they were written against the German UI)."""
import os

os.environ.setdefault("STREAMLIN_LANG", "de")

# Tests never contact GitHub for updates
os.environ.setdefault("STREAMLIN_NO_UPDATE_CHECK", "1")
