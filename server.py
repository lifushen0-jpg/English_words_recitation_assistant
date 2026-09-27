from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from learning_service import DeepSeekError, LearningService, ValidationError
from storage import JsonVocabularyRepository, NotFoundError

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DATABASE = ROOT / "VocabularyDatabase"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # DeepSeek 配置由业务层按请求动态读取，避免文件修改后残留旧密钥。
        if key.startswith("DEEPSEEK_"):
            continue
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")
repository = JsonVocabularyRepository(DATABASE)
service = LearningService(repository)
SHUTDOWN_TOKEN = secrets.token_urlsafe(32)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """拒绝同一端口上的重复实例，避免请求随机命中不同服务。"""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


class VocabularyRequestHandler(BaseHTTPRequestHandler):
    server_version = "VocabularyMVP/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        sys.stdout.write(f"{self.log_date_time_string()} - {format_string % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self.send_json({"ok": True})
            elif parsed.path == "/api/dashboard":
                dashboard = service.get_dashboard()
                dashboard["shutdown_token"] = SHUTDOWN_TOKEN
                self.send_json(dashboard)
            elif parsed.path in {"/api/sessions/learning", "/api/sessions/review"}:
                self.send_json(service.get_session(parsed.path.rsplit("/", 1)[-1]))
            elif parsed.path.startswith("/api/words/"):
                word = unquote(parsed.path.removeprefix("/api/words/"))
                self.send_json(service.get_word_details(word))
            else:
                self.serve_static(parsed.path)
        except Exception as exc:
            self.handle_exception(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self.read_json()
            if parsed.path == "/api/deepseek/check":
                self.send_json(service.check_deepseek_connection())
            elif parsed.path == "/api/server/shutdown":
                supplied_token = self.headers.get("X-Shutdown-Token", "")
                if not secrets.compare_digest(supplied_token, SHUTDOWN_TOKEN):
                    self.send_json({"error": "关闭凭证无效"}, HTTPStatus.FORBIDDEN)
                    return
                self.send_json({"ok": True, "message": "服务器正在关闭"})
                threading.Thread(
                    target=self.server.shutdown,
                    name="wordroot-shutdown",
                    daemon=True,
                ).start()
            elif parsed.path == "/api/learning-plan/generate":
                self.send_json(service.generate_learning_plan(), HTTPStatus.CREATED)
            elif parsed.path in {
                "/api/sessions/learning/answer",
                "/api/sessions/review/answer",
            }:
                self.send_json(service.record_answer(parsed.path.split("/")[-2], body))
            elif parsed.path.startswith("/api/words/") and parsed.path.endswith("/know-well"):
                word = unquote(
                    parsed.path.removeprefix("/api/words/").removesuffix("/know-well")
                )
                self.send_json(service.mark_know_well(word, body))
            else:
                self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_exception(exc)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("无效的请求长度") from exc
        if length > 1_000_000:
            raise ValidationError("请求内容过大")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求不是有效的 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求 JSON 必须是对象")
        return value

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        candidate = (FRONTEND / relative).resolve()
        try:
            candidate.relative_to(FRONTEND.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = candidate.read_bytes()
        mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, NotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, (ValidationError, ValueError, json.JSONDecodeError)):
            status = HTTPStatus.BAD_REQUEST
        elif isinstance(exc, DeepSeekError):
            status = HTTPStatus.BAD_GATEWAY
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            self.log_error("Unhandled error: %r", exc)
        message = str(exc) if status != HTTPStatus.INTERNAL_SERVER_ERROR else "服务器内部错误"
        self.send_json({"error": message}, status)


def main() -> None:
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    try:
        server = ExclusiveThreadingHTTPServer((host, port), VocabularyRequestHandler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048:
            print(f"无法启动：端口 {port} 已有一个单词学习服务在运行。")
            raise SystemExit(1) from None
        raise
    print(f"单词学习程序已启动：http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n程序已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
