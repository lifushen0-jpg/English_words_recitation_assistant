from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from storage import VocabularyRepository, new_learning_state, normalize_word_name

CHINA_TIMEZONE = timezone(timedelta(hours=8))
FEEDBACK_DAYS = {"hard": 1, "good": 3, "easy": 7}
VALID_MODES = {"learning", "review"}
ENV_PATH = Path(__file__).resolve().parent / ".env"


class ValidationError(Exception):
    pass


class DeepSeekError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class LearningService:
    def __init__(
        self,
        repository: VocabularyRepository,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self._now = now or (lambda: datetime.now(CHINA_TIMEZONE))

    def get_dashboard(self) -> dict[str, Any]:
        learning = self.repository.get_learning_plan()
        review = self.repository.get_review_plan()
        return {
            "learning": self._plan_summary(learning),
            "review": {
                "due_count": len(self._due_review_words(review)),
                "scheduled_count": len(review.get("items", [])),
                "session_active": bool(
                    review.get("active_session")
                    and review["active_session"].get("status") == "in_progress"
                ),
            },
            "total_words": len(self.repository.list_words()),
            "deepseek_configured": bool(config_value("DEEPSEEK_API_KEY")),
        }

    def get_word_details(self, word: str) -> dict[str, Any]:
        normalized = normalize_word_name(word)
        return {
            "word": normalize_word_data(self.repository.get_word(normalized)),
            "state": self.repository.get_state(normalized),
        }

    def get_session(self, mode: str) -> dict[str, Any]:
        self._validate_mode(mode)
        if mode == "learning":
            session = self._learning_session_view(self.repository.get_learning_plan())
        else:
            plan = self.repository.get_review_plan()
            active = plan.get("active_session")
            if not active or active.get("status") != "in_progress":
                due_words = self._due_review_words(plan)
                active = {
                    "started_at": self._iso_now(),
                    "status": "in_progress" if due_words else "completed",
                    "words": due_words.copy(),
                    "queue": due_words,
                    "current_index": 0,
                    "completed_words": [],
                }
                plan["active_session"] = active
                self.repository.save_review_plan(plan)
            session = self._review_session_view(plan)
        session["card"] = (
            normalize_word_data(self.repository.get_word(session["current_word"]))
            if session.get("current_word")
            else None
        )
        return session

    def record_answer(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_mode(mode)
        word = normalize_word_name(str(payload.get("word", "")))
        word_data = normalize_word_data(self.repository.get_word(word))
        expected_ids = {meaning["id"] for meaning in word_data["meanings"]}
        provided = payload.get("meaning_results")
        if not isinstance(provided, dict) or set(provided) != expected_ids:
            raise ValidationError("必须对每个释义分别作出正确或错误的判断")
        if any(type(value) is not bool for value in provided.values()):
            raise ValidationError("释义判断必须是布尔值")
        if self._current_word(mode) != word:
            raise ValidationError("提交的单词不是当前卡片，请刷新后重试")

        correct = all(provided.values())
        now = self._now()
        state = self.repository.get_state(word)
        if state.get("first_learning_time") is None:
            state["first_learning_time"] = now.isoformat(timespec="seconds")
        records = state.setdefault("learning_records", [])
        feedback = calculate_feedback(records, correct)
        due_time = calculate_due_time(now, feedback)
        records.append(
            {
                "time": now.isoformat(timespec="seconds"),
                "mode": mode,
                "correct": correct,
                "meaning_results": provided,
                "feedback_after": feedback,
            }
        )
        state.update(
            {
                "schema_version": 1,
                "feedback": feedback,
                "next_review_time": due_time,
                "manually_known_well": False,
            }
        )
        self.repository.save_state(state)
        self._update_review_item(word, feedback, due_time)
        self._advance_session(mode, word, append_again=not correct)
        return {
            "result": {
                "correct": correct,
                "feedback": feedback,
                "next_review_time": due_time,
            },
            "session": self.get_session(mode),
        }

    def mark_know_well(self, word: str, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode", ""))
        self._validate_mode(mode)
        normalized = normalize_word_name(word)
        if self._current_word(mode) != normalized:
            raise ValidationError("只能将当前卡片设为熟练掌握")
        now = self._iso_now()
        state = self.repository.get_state(normalized)
        if state.get("first_learning_time") is None:
            state["first_learning_time"] = now
        state.setdefault("learning_records", []).append(
            {
                "time": now,
                "mode": mode,
                "correct": True,
                "meaning_results": {},
                "feedback_after": "know_well",
                "manual_override": True,
            }
        )
        state.update(
            {
                "feedback": "know_well",
                "next_review_time": None,
                "manually_known_well": True,
            }
        )
        self.repository.save_state(state)
        self._update_review_item(normalized, "know_well", None)
        self._advance_session(mode, normalized, append_again=False)
        return {
            "result": {"correct": True, "feedback": "know_well", "next_review_time": None},
            "session": self.get_session(mode),
        }

    def generate_learning_plan(self) -> dict[str, Any]:
        existing = self.repository.list_words()
        last_error: DeepSeekError | None = None
        validated: list[dict[str, Any]] = []
        names: list[str] = []
        for _ in range(2):
            try:
                generated = request_deepseek_words(existing)
                validated = [validate_generated_word(item) for item in generated]
                names = [item["word"] for item in validated]
                if len(validated) != 10 or len(set(names)) != 10:
                    raise DeepSeekError("DeepSeek 必须返回10个不同的单词")
                if set(names).intersection(existing):
                    raise DeepSeekError("DeepSeek 返回了数据库中已有的单词")
                break
            except DeepSeekError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        else:
            raise DeepSeekError(f"DeepSeek 连续两次生成失败：{last_error}") from last_error
        for word_data in validated:
            self.repository.save_word(word_data)
            self.repository.save_state(new_learning_state(word_data["word"]))
        plan = {
            "schema_version": 1,
            "generated_at": self._iso_now(),
            "status": "in_progress",
            "words": names,
            "queue": names.copy(),
            "current_index": 0,
            "completed_words": [],
        }
        self.repository.save_learning_plan(plan)
        return {"plan": self._plan_summary(plan), "words": names}

    def check_deepseek_connection(self) -> dict[str, Any]:
        api_key = config_value("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValidationError("尚未配置 DEEPSEEK_API_KEY，请先填写 .env 文件")
        payload = {
            "model": config_value("DEEPSEEK_MODEL", "deepseek-chat"),
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "temperature": 0,
            "max_tokens": 8,
        }
        result = perform_deepseek_request(payload, api_key, timeout=30)
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek 连接成功，但响应结构异常") from exc
        return {
            "ok": True,
            "model": result.get("model", payload["model"]),
            "message": f"连接成功（响应：{str(content).strip()[:40]}）",
        }

    def _current_word(self, mode: str) -> str | None:
        if mode == "learning":
            return self._learning_session_view(self.repository.get_learning_plan()).get("current_word")
        return self._review_session_view(self.repository.get_review_plan()).get("current_word")

    def _advance_session(self, mode: str, word: str, append_again: bool) -> None:
        if mode == "learning":
            plan = self.repository.get_learning_plan()
            queue = plan.setdefault("queue", [])
            completed = derive_completed_words(plan)
            if append_again:
                queue.append(word)
            elif word not in completed:
                completed.append(word)
            plan["completed_words"] = completed
            plan["current_index"] = int(plan.get("current_index", 0)) + 1
            plan["status"] = "completed" if plan["current_index"] >= len(queue) else "in_progress"
            self.repository.save_learning_plan(plan)
            return
        plan = self.repository.get_review_plan()
        active = plan.get("active_session") or {}
        queue = active.setdefault("queue", [])
        active.setdefault("words", list(dict.fromkeys(queue)))
        completed = derive_completed_words(active)
        if append_again:
            queue.append(word)
        elif word not in completed:
            completed.append(word)
        active["completed_words"] = completed
        active["current_index"] = int(active.get("current_index", 0)) + 1
        active["status"] = "completed" if active["current_index"] >= len(queue) else "in_progress"
        plan["active_session"] = active
        self.repository.save_review_plan(plan)

    def _update_review_item(self, word: str, feedback: str, due_time: str | None) -> None:
        plan = self.repository.get_review_plan()
        items = [item for item in plan.get("items", []) if item.get("word") != word]
        if feedback != "know_well" and due_time:
            items.append({"word": word, "due_time": due_time, "feedback": feedback})
        items.sort(key=lambda item: item["due_time"])
        plan["items"] = items
        self.repository.save_review_plan(plan)

    def _due_review_words(self, plan: dict[str, Any]) -> list[str]:
        now = self._now()
        due: list[tuple[datetime, str]] = []
        for item in plan.get("items", []):
            try:
                due_time = datetime.fromisoformat(item["due_time"])
                if due_time.tzinfo is None:
                    due_time = due_time.replace(tzinfo=CHINA_TIMEZONE)
                if due_time <= now:
                    due.append((due_time, normalize_word_name(item["word"])))
            except (KeyError, TypeError, ValueError):
                continue
        due.sort(key=lambda pair: pair[0])
        return [word for _, word in due]

    @staticmethod
    def _learning_session_view(plan: dict[str, Any]) -> dict[str, Any]:
        queue, index = plan.get("queue", []), int(plan.get("current_index", 0))
        current = queue[index] if 0 <= index < len(queue) else None
        words = plan.get("words") or list(dict.fromkeys(queue))
        completed = derive_completed_words(plan)
        return {
            "mode": "learning",
            "status": "in_progress" if current else plan.get("status", "idle"),
            "current_word": current,
            "position": len(completed),
            "completed": len(completed),
            "total": len(words),
            "remaining": max(len(words) - len(completed), 0),
        }

    @staticmethod
    def _review_session_view(plan: dict[str, Any]) -> dict[str, Any]:
        active = plan.get("active_session") or {}
        queue, index = active.get("queue", []), int(active.get("current_index", 0))
        current = queue[index] if 0 <= index < len(queue) else None
        words = active.get("words") or list(dict.fromkeys(queue))
        completed = derive_completed_words(active)
        return {
            "mode": "review",
            "status": "in_progress" if current else active.get("status", "completed"),
            "current_word": current,
            "position": len(completed),
            "completed": len(completed),
            "total": len(words),
            "remaining": max(len(words) - len(completed), 0),
        }

    @staticmethod
    def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
        queue = plan.get("queue", [])
        words = plan.get("words") or list(dict.fromkeys(queue))
        completed = derive_completed_words(plan)
        return {
            "status": plan.get("status", "idle"),
            "word_count": len(words),
            "completed": len(completed),
            "remaining": max(len(words) - len(completed), 0),
            "generated_at": plan.get("generated_at"),
        }

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValidationError("学习模式必须是 learning 或 review")


def calculate_feedback(records: list[dict[str, Any]], correct: bool) -> str:
    if not correct:
        return "again"
    streak = 1
    for record in reversed(records):
        if not record.get("correct", False):
            break
        streak += 1
    return {1: "hard", 2: "good", 3: "easy"}.get(streak, "know_well")


def derive_completed_words(session: dict[str, Any]) -> list[str]:
    """Return mastered words without letting retry queue entries inflate progress."""
    queue = session.get("queue", [])
    original = session.get("words") or list(dict.fromkeys(queue))
    stored = session.get("completed_words")
    if isinstance(stored, list):
        stored_set = set(stored)
        return [word for word in original if word in stored_set]
    index = int(session.get("current_index", 0))
    processed = set(queue[:index])
    pending = set(queue[index:])
    return [word for word in original if word in processed and word not in pending]


def calculate_due_time(now: datetime, feedback: str) -> str | None:
    if feedback == "know_well":
        return None
    return (now + timedelta(days=FEEDBACK_DAYS.get(feedback, 0))).isoformat(timespec="seconds")


def normalize_word_data(raw: dict[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    meanings: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("meanings", []), start=1):
        if not isinstance(item, dict):
            continue
        examples = item.get("examples")
        if not isinstance(examples, list):
            legacy = item.get("example")
            examples = [legacy] if isinstance(legacy, str) else []
        meanings.append(
            {
                "id": str(item.get("id") or f"meaning_{index}"),
                "part_of_speech": str(item.get("part_of_speech", "")),
                "meaning_en": str(item.get("meaning_en", "")),
                "meaning_zh": str(item.get("meaning_zh", "")),
                "examples": [str(example) for example in examples if str(example).strip()],
            }
        )
    value["meanings"] = meanings
    return value


def validate_generated_word(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeepSeekError("DeepSeek 返回了无效的单词数据")
    try:
        word = normalize_word_name(str(raw.get("word", "")))
    except ValueError as exc:
        raise DeepSeekError(str(exc)) from exc
    normalized = normalize_word_data(raw)
    normalized.update({"schema_version": 1, "word": word})
    if not str(normalized.get("phonetic", "")).strip():
        raise DeepSeekError(f"{word} 缺少音标")
    morphology = normalized.get("morphology")
    if not isinstance(morphology, dict) or not morphology.get("analysis"):
        raise DeepSeekError(f"{word} 缺少词根词缀分析")
    if not normalized["meanings"]:
        raise DeepSeekError(f"{word} 缺少释义")
    for meaning in normalized["meanings"]:
        if not meaning["meaning_en"] or not meaning["meaning_zh"] or not meaning["examples"]:
            raise DeepSeekError(f"{word} 的释义或例句不完整")
        if len(meaning["examples"]) != 1:
            raise DeepSeekError(f"{word} 的每个独立释义必须恰好包含一个例句")
    signatures = {
        re.sub(r"[^a-z0-9]", "", meaning["meaning_en"].lower())
        for meaning in normalized["meanings"]
    }
    if len(signatures) != len(normalized["meanings"]):
        raise DeepSeekError(f"{word} 包含重复释义")
    return normalized


def request_deepseek_words(existing_words: list[str]) -> list[Any]:
    api_key = config_value("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValidationError("尚未配置 DEEPSEEK_API_KEY，请先填写 .env 文件")
    model = config_value("DEEPSEEK_MODEL", "deepseek-chat")
    excluded = ", ".join(existing_words) if existing_words else "无"
    prompt = f"""推荐10个不重复、适合中国大学英语六级（CET-6）的英语单词。不得返回：{excluded}
只返回 JSON，不要 Markdown。根节点为 {{"words": [...]}}，数组中每项结构如下：
{{"word":"lowercase","phonetic":"/IPA/","morphology":{{"parts":[{{"form":"pre-","type":"prefix","meaning_en":"before","meaning_zh":"在前"}}],"analysis":["逐步、准确的构词分析"]}},"meanings":[{{"id":"meaning_1","part_of_speech":"verb","meaning_en":"English definition","meaning_zh":"中文释义","examples":["体现该释义的自然例句"]}}]}}
词根词缀分析必须准确；不可简单拆分的词要明确说明词源。
释义划分规则：
1. meanings 中只列语义真正不同的常用 CET-6 词义。
2. 近义表述、同一核心概念的改写、仅使用场景不同但含义相同的释义必须合并，不能拆成两项。
3. “获胜”与“盛行”这类语义不同的用法必须拆成两项。
4. 每个独立释义恰好提供一个 examples 例句，不多不少。因此一个词有几个独立释义，全部卡片合计就有几个例句。
5. 输出前先在内部比较同一单词的全部释义并去重，但不要输出检查过程。"""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是严谨的英语词汇与词源编辑，只输出有效 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "max_tokens": int(config_value("DEEPSEEK_MAX_TOKENS", "16000")),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    payload = json.loads(body.decode("utf-8"))
    try:
        result = perform_deepseek_request(payload, api_key, timeout=120)
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            raise DeepSeekError("DeepSeek 输出因长度限制被截断；请提高 DEEPSEEK_MAX_TOKENS")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("DeepSeek 返回了空内容")
        parsed = parse_model_json(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise DeepSeekError("DeepSeek 返回的数据无法解析或不是完整 JSON") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("words")
    if not isinstance(parsed, list):
        raise DeepSeekError("DeepSeek 返回内容必须包含 words 数组")
    return parsed


def parse_model_json(content: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def config_value(name: str, default: str = "") -> str:
    """每次请求重新读取 .env，修改密钥后不必重启服务器。"""
    file_values: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            file_values[key.strip()] = value.strip().strip('"').strip("'")
    if name in file_values:
        return file_values[name]
    return os.getenv(name, default).strip()


def perform_deepseek_request(
    payload: dict[str, Any], api_key: str, *, timeout: int
) -> dict[str, Any]:
    request = urllib.request.Request(
        config_value("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw_detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw_detail).get("error", {}).get("message", raw_detail)
        except json.JSONDecodeError:
            detail = raw_detail
        friendly = {
            400: "请求格式或模型参数不被接受",
            401: "API 密钥无效",
            402: "DeepSeek 账户余额不足",
            422: "请求参数校验失败",
            429: "请求过于频繁，请稍后再试",
            500: "DeepSeek 服务器内部错误",
            503: "DeepSeek 服务繁忙",
        }.get(exc.code, "API 请求失败")
        retryable = exc.code in {429, 500, 502, 503, 504}
        raise DeepSeekError(
            f"{friendly}（HTTP {exc.code}）：{str(detail)[:300]}",
            retryable=retryable,
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DeepSeekError(f"无法连接 DeepSeek：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise DeepSeekError("DeepSeek 返回的响应不是有效 JSON") from exc
    if not isinstance(result, dict):
        raise DeepSeekError("DeepSeek 返回的响应结构异常")
    return result
