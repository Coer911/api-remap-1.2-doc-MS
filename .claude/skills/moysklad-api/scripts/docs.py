#!/usr/bin/env python3
"""Поиск по документации МойСклад JSON API 1.2 — копии в docs/moysklad-api этого репозитория.

  python3 .claude/skills/moysklad-api/scripts/docs.py отгрузк печать
      — разделы, где встречаются ВСЕ слова (без учёта регистра), с путём заголовков
  python3 .claude/skills/moysklad-api/scripts/docs.py --show documents/_print.md:13
      — целиком раздел, в котором стоит эта строка
  python3 .claude/skills/moysklad-api/scripts/docs.py --toc documents/_demand.md
      — оглавление файла

Документация — источник правды: поля, фильтры и ограничения API меняются,
а память модели о них устаревает.
"""
import re
import signal
import sys
from pathlib import Path

signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # docs.py … | head — без трейсбэка
ROOT = Path(__file__).resolve().parents[4] / "docs" / "moysklad-api"
HEAD = re.compile(r"^(#{1,5})\s+(.*\S)\s*$")


def sections(path: Path):
    """[(строка начала, путь заголовков, строки раздела)]"""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, trail, start, buf = [], [], 1, []
    for i, line in enumerate(lines, 1):
        m = HEAD.match(line)
        if m:
            if buf:
                out.append((start, " › ".join(t for _, t in trail), buf))
            level = len(m.group(1))
            trail = [t for t in trail if t[0] < level] + [(level, m.group(2))]
            start, buf = i, [line]
        else:
            buf.append(line)
    if buf:
        out.append((start, " › ".join(t for _, t in trail), buf))
    return out


def search(words):
    words = [w.lower() for w in words]
    hits = 0
    for path in sorted(ROOT.rglob("*.md")):
        for start, trail, buf in sections(path):
            text = "\n".join(buf).lower()
            if all(w in text for w in words):
                hits += 1
                line = next((start + k for k, ln in enumerate(buf) if words[0] in ln.lower()), start)
                snippet = next((ln.strip() for ln in buf if words[0] in ln.lower()), "")[:140]
                print(f"{path.relative_to(ROOT)}:{line}  {trail}\n    {snippet}")
                if hits >= 40:
                    print("… больше 40 совпадений — уточните запрос")
                    return
    if not hits:
        print("ничего не найдено — попробуйте корень слова (отгрузк, публикац) или англ. имя поля")


def doc(file):
    path = ROOT / file
    if not path.is_file():
        sys.exit(f"нет файла {file} — пути от docs/moysklad-api, например documents/_demand.md")
    return path


def show(ref):
    file, _, line = ref.partition(":")
    line = int(line or 1)
    for start, trail, buf in sections(doc(file)):
        if start <= line < start + len(buf):
            print(f"# {file}:{start}  {trail}\n")
            print("\n".join(buf))
            return


def toc(file):
    for start, trail, _ in sections(doc(file)):
        print(f"{start:>5}  {trail}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
    elif args[0] == "--show":
        show(args[1])
    elif args[0] == "--toc":
        toc(args[1])
    else:
        search(args)
