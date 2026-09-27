from __future__ import annotations

import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "WordRoot-share.zip"
ARCHIVE_ROOT = "WordRoot"

ROOT_FILES = [
    ".env.example",
    ".gitignore",
    "README.md",
    "learning_service.py",
    "server.py",
    "start_wordroot.bat",
    "storage.py",
    "使用说明.txt",
]


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def fresh_state(word: str) -> dict:
    return {
        "schema_version": 1,
        "word": word,
        "first_learning_time": None,
        "learning_records": [],
        "feedback": "new",
        "next_review_time": None,
        "manually_known_well": False,
    }


def add_file(archive: zipfile.ZipFile, source: Path, relative: Path) -> None:
    archive.write(source, f"{ARCHIVE_ROOT}/{relative.as_posix()}")


def main() -> None:
    word_files = sorted((ROOT / "VocabularyDatabase" / "words").glob("*.json"))
    words = [path.stem for path in word_files]
    starter = "predict" if "predict" in words else (words[0] if words else None)
    learning_plan = {
        "schema_version": 1,
        "generated_at": None,
        "status": "in_progress" if starter else "idle",
        "words": [starter] if starter else [],
        "queue": [starter] if starter else [],
        "current_index": 0,
        "completed_words": [],
    }
    review_plan = {"schema_version": 1, "items": [], "active_session": None}

    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ROOT_FILES:
            source = ROOT / name
            if source.is_file():
                add_file(archive, source, Path(name))
        for source in sorted((ROOT / "frontend").rglob("*")):
            if source.is_file():
                add_file(archive, source, source.relative_to(ROOT))
        for source in word_files:
            add_file(archive, source, source.relative_to(ROOT))
        for word in words:
            archive.writestr(
                f"{ARCHIVE_ROOT}/VocabularyDatabase/learning_state/{word}.json",
                json_bytes(fresh_state(word)),
            )
        archive.writestr(
            f"{ARCHIVE_ROOT}/VocabularyDatabase/LearningPlan.json",
            json_bytes(learning_plan),
        )
        archive.writestr(
            f"{ARCHIVE_ROOT}/VocabularyDatabase/ReviewPlan.json",
            json_bytes(review_plan),
        )

    print(f"分享包已创建：{OUTPUT}")
    print("其中不包含 .env、API 密钥或个人学习记录。")


if __name__ == "__main__":
    main()
