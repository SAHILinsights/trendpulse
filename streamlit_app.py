"""Streamlit Community Cloud entry point."""

import sys
from pathlib import Path

# Make the src/ layout importable even if the package was not pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from trendpulse.dashboard.app import main  # noqa: E402

main()
