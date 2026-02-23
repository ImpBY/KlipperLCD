from pathlib import Path
import sys

# Allow running from repository root without package installation.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from klipperlcd.app import run


if __name__ == "__main__":
    run()
