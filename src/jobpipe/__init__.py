"""jobpipe: tailor a resume and cover letter to a job posting, audit them for ATS and
track applications."""

from pathlib import Path

__version__ = "0.1.0"


def resource_dir(name: str) -> Path:
    """Locate a bundled resource folder (prompts/ or templates/).

    Installed wheels carry them inside the package; a source checkout keeps them at the
    repository root so they are easy to edit.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / name, here.parents[1] / name):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"Cannot find the '{name}' folder next to {here}")
