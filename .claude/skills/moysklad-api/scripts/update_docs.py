#!/usr/bin/env python3
"""Обновить docs/moysklad-api из официальной документации МойСклад.

  python3 .claude/skills/moysklad-api/scripts/update_docs.py [ветка]

Берёт md/ (раньше source/includes) из github.com/moysklad/api-remap-1.2-doc (по умолчанию —
ветка по умолчанию репозитория), заменяет ими docs/moysklad-api и пишет коммит
в SOURCE.txt. Дальше — git diff docs/moysklad-api: что поменялось в API.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/moysklad/api-remap-1.2-doc"
DEST = Path(__file__).resolve().parents[4] / "docs" / "moysklad-api"


def main() -> None:
    branch = sys.argv[1:] and ["--branch", sys.argv[1]] or []
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--depth", "1", *branch, REPO, str(src)], check=True)
        commit = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        # раскладка менялась: сейчас md/, раньше source/includes
        found = next((src / d for d in ("md", "source/includes") if (src / d).is_dir()), None)
        if found is None or not any(found.rglob("*.md")):
            sys.exit("в репозитории документации не нашлось md/ или source/includes — ничего не меняю")
        staged = Path(tmp) / "new"
        shutil.copytree(found, staged)
        shutil.rmtree(DEST, ignore_errors=True)      # старую копию — только когда новая готова
        shutil.move(str(staged), DEST)
    (DEST / "SOURCE.txt").write_text(
        f"Документация МойСклад JSON API 1.2 — копия md/ из\n{REPO}.\n"
        f"Коммит: {commit}\nОбновить: python3 .claude/skills/moysklad-api/scripts/update_docs.py\n",
        encoding="utf-8")
    print(f"docs/moysklad-api обновлена до {commit[:12]} — смотрите git diff docs/moysklad-api")


if __name__ == "__main__":
    main()
