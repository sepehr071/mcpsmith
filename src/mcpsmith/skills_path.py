from importlib.resources import files
from pathlib import Path


def skills_dir() -> Path:
    """The skills folder: packaged in a wheel, or the repo's `skills/` in an editable install."""
    packaged = Path(str(files("mcpsmith") / "skills"))
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "skills"
