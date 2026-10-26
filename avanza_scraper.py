import argparse
import json
import os
import tempfile
from pathlib import Path

from auth.sessions_manager import SessionsManager


def _save_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(value, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def run_scraper(output_file: Path) -> None:
    session = SessionsManager().get_avanza_session()
    positions = session.get_accounts_positions()
    _save_private_json(output_file, positions)
    print(f"Avanza account positions saved to {output_file}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch Avanza account positions and save them as private JSON."
    )
    parser.add_argument(
        "--output", type=Path, default=Path("avanza_positions.json")
    )
    run_scraper(parser.parse_args().output)
