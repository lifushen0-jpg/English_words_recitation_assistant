from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from threading import RLock
from typing import Any

WORD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z'-]*$")


class NotFoundError(Exception):
    pass


class VocabularyRepository(ABC):
    """持久化边界：未来换数据库时实现同一接口即可。"""

    @abstractmethod
    def list_words(self) -> list[str]: ...

    @abstractmethod
    def get_word(self, word: str) -> dict[str, Any]: ...

    @abstractmethod
    def save_word(self, word: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_state(self, word: str) -> dict[str, Any]: ...

    @abstractmethod
    def save_state(self, state: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_learning_plan(self) -> dict[str, Any]: ...

    @abstractmethod
    def save_learning_plan(self, plan: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_review_plan(self) -> dict[str, Any]: ...

    @abstractmethod
    def save_review_plan(self, plan: dict[str, Any]) -> None: ...


def empty_learning_plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": None,
        "status": "idle",
        "words": [],
        "queue": [],
        "current_index": 0,
        "completed_words": [],
    }


def empty_review_plan() -> dict[str, Any]:
    return {"schema_version": 1, "items": [], "active_session": None}


def new_learning_state(word: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "word": word,
        "first_learning_time": None,
        "learning_records": [],
        "feedback": "new",
        "next_review_time": None,
        "manually_known_well": False,
    }


class JsonVocabularyRepository(VocabularyRepository):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.words_dir = self.root / "words"
        self.states_dir = self.root / "learning_state"
        self.learning_plan_path = self.root / "LearningPlan.json"
        self.review_plan_path = self.root / "ReviewPlan.json"
        self._lock = RLock()
        self.words_dir.mkdir(parents=True, exist_ok=True)
        self.states_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_json(self.learning_plan_path, empty_learning_plan())
        self._initialize_json(self.review_plan_path, empty_review_plan())

    def list_words(self) -> list[str]:
        return sorted(path.stem for path in self.words_dir.glob("*.json"))

    def get_word(self, word: str) -> dict[str, Any]:
        path = self._word_path(self.words_dir, word)
        if not path.exists():
            raise NotFoundError(f"找不到单词：{word}")
        return self._read_json(path)

    def save_word(self, word: dict[str, Any]) -> None:
        normalized = normalize_word_name(str(word.get("word", "")))
        value = dict(word)
        value["word"] = normalized
        self._atomic_write(self._word_path(self.words_dir, normalized), value)

    def get_state(self, word: str) -> dict[str, Any]:
        normalized = normalize_word_name(word)
        path = self._word_path(self.states_dir, normalized)
        if not path.exists():
            return new_learning_state(normalized)
        return self._read_json(path)

    def save_state(self, state: dict[str, Any]) -> None:
        normalized = normalize_word_name(str(state.get("word", "")))
        value = dict(state)
        value["word"] = normalized
        self._atomic_write(self._word_path(self.states_dir, normalized), value)

    def get_learning_plan(self) -> dict[str, Any]:
        return self._read_json(self.learning_plan_path)

    def save_learning_plan(self, plan: dict[str, Any]) -> None:
        self._atomic_write(self.learning_plan_path, plan)

    def get_review_plan(self) -> dict[str, Any]:
        return self._read_json(self.review_plan_path)

    def save_review_plan(self, plan: dict[str, Any]) -> None:
        self._atomic_write(self.review_plan_path, plan)

    def _initialize_json(self, path: Path, default: dict[str, Any]) -> None:
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            self._atomic_write(path, default)

    def _read_json(self, path: Path) -> dict[str, Any]:
        with self._lock, path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise json.JSONDecodeError("JSON 根节点必须是对象", str(value), 0)
        return value

    def _atomic_write(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    @staticmethod
    def _word_path(directory: Path, word: str) -> Path:
        return directory / f"{normalize_word_name(word)}.json"


def normalize_word_name(word: str) -> str:
    normalized = word.strip().lower()
    if not WORD_PATTERN.fullmatch(normalized):
        raise ValueError("单词只能包含英文字母、连字符或撇号")
    return normalized
