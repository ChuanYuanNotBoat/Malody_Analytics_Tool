from pathlib import Path
import sys


REQUIRED_DOCS = [
    "README.md",
    "docs/API_CONTRACT.md",
    "docs/EXCLUSIONS.md",
    "docs/I18N.md",
    "docs/FIRST_COMMIT_PREP.md",
]

REQUIRED_GITIGNORE_PATTERNS = [
    "config/settings.json",
    "logs/tasks/*.jsonl",
    "build/",
    "dist/",
    "__pycache__/",
]


def fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = 0

    for rel in REQUIRED_DOCS:
        path = root / rel
        if not path.exists():
            errors += fail(f"missing required doc: {rel}")
            continue
        try:
            _ = path.read_text(encoding="utf-8")
        except Exception as exc:
            errors += fail(f"doc is not readable as utf-8: {rel} ({exc})")

    readme = (root / "README.md").read_text(encoding="utf-8")
    if "ui_language" not in readme or "zh_en" not in readme or "en" not in readme:
        errors += fail("README missing ui_language supported-value documentation")

    exclusions = (root / "docs" / "EXCLUSIONS.md").read_text(encoding="utf-8")
    for section in ("Backend Scope", "GUI Scope", "Engineering Scope"):
        if section not in exclusions:
            errors += fail(f"EXCLUSIONS missing section: {section}")

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        if pattern not in gitignore:
            errors += fail(f".gitignore missing pattern: {pattern}")

    trans_dir = root / "translations"
    for filename in ("malody_zh_CN.ts", "malody_zh_CN.qm"):
        if not (trans_dir / filename).exists():
            errors += fail(f"translations missing file: {filename}")

    if errors:
        print(f"[SUMMARY] verify_repo_docs failed with {errors} error(s)")
        return 1
    print("[OK] verify_repo_docs passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
