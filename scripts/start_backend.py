"""Start the Flask backend with the project's local dependencies when available."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"


def add_project_paths():
    """Make the backend and an existing local virtualenv importable."""
    candidates = [
        BACKEND_ROOT / "venv" / "Lib" / "site-packages",
        BACKEND_ROOT / "venv" / "lib" / "python3.12" / "site-packages",
    ]
    for path in [BACKEND_ROOT, *candidates]:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def main():
    add_project_paths()
    try:
        from app import create_app
        from config import Config
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Flask dependencies are unavailable. Run "
            "python -m pip install -r backend/requirements.txt first."
        ) from exc

    create_app(Config).run(
        host=Config.FLASK_HOST,
        port=Config.FLASK_PORT,
        debug=Config.FLASK_DEBUG,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
