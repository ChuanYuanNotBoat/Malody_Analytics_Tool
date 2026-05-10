import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build helper for Malody Analytics Desktop")
    parser.add_argument("--skip-tests", action="store_true", help="Skip test run before packaging")
    parser.add_argument("--name", default="MalodyAnalyticsDesktop", help="Executable name")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent

    if not args.skip_tests:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"])

    run([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--name",
            args.name,
            "--windowed",
            "--collect-all",
            "PySide6",
            "--hidden-import",
            "openpyxl",
            "--add-data",
            "docs;docs",
            "--add-data",
            "translations;translations",
            "--add-data",
            "resources_rc.py;.",
            str(project_root / "main.py"),
        ]
    )
    print(f"Build completed: dist/{args.name}/{args.name}.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
