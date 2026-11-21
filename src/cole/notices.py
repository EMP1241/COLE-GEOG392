"""Locate the adopted legal documents in source, Windows and wheel installs."""
from importlib.metadata import distribution
from pathlib import Path
import sys


def notice_path(name):
    if name not in {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
        raise ValueError("Unknown legal document")
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / name
    source = Path(__file__).resolve().parents[2] / name
    if source.is_file():
        return source
    # Modern wheels include license-files in their distribution metadata.
    installed = distribution("cole-app")
    for entry in installed.files or ():
        if entry.name == name and ".dist-info/licenses/" in entry.as_posix():
            return Path(installed.locate_file(entry))
    raise FileNotFoundError(f"Missing bundled legal document: {name}")
