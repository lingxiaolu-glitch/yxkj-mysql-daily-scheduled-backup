"""通知适配器单元测试：日志、SMTP、Webhook 与失败静默。"""

# 延迟类型注解。
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.events import (
    BackupRunCompleted,
    DatabaseBackupFailed,
    DatabaseBackupSucceeded,
    DomainEvent,
)
from domain.model.value_objects import (
    BackupTime,
    Compression,
    DbName,
    FileName,
    RunStatus,
    SizeBytes,
)
from infrastructure.config_loader import NotifyConfig, NotifyType
from infrastructure.notifiers import (
    LogNotifier,
    NoopNotifier,
    NotifierConfigError,
    SmtpNotifier,
    WebhookNotifier,
    create_notifier,
)


def _time() -> BackupTime:
    zone = timezone(timedelta(hours=8))
    return BackupTime(datetime(2026, 9, 5, 2, 0, 0, tzinfo=zone))


def _file() -> FileName:
    return FileName(DbName("shop"), _time(), Compression.GZIP)


def _failure_event(secret: str = "topsecret") -> DatabaseBackupFailed:
    return DatabaseBackupFailed(
        run_id="run-001",
        occurred_at=_time(),
        db_name=DbName("shop"),
        attempts=1,
        will_retry=False,
        error_digest=f"Access denied: {secret}",
        elapsed_seconds=1.2,
    )


def _success_event() -> DatabaseBackupSucceeded:
    return DatabaseBackupSucceeded(
        run_id="run-001",
        occurred_at=_time(),
        db_name=DbName("shop"),
        file_name=_file(),
        size_bytes=SizeBytes(10),
        elapsed_seconds=1.2,
    )


class _FakeLogger:
    """记录日志调用的简单 logger。"""

    def __init__(self) -> None:
        self.records: list[tuple[int, str]] = []

    def log(self, level: int, message: str) -> None:
        self.records.append((level, message))


class _FakeSmtp:
    """记录 SMTP 交互的 fake transport。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.login_called = False
        self.messages = []
        self.quit_called = False

    def login(self, username: str, password: str) -> None:
        self.login_called = True
        self.username = username
        self.password = password

    def send_message(self, message) -> None:
        self.messages.append(message)

    def quit(self) -> None:
        self.quit_called = True


class _FakeResponse:
    """支持 with 的简单响应对象。"""

    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


class _FakeOpener:
    """记录 Webhook 请求的 fake urlopen。"""

    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        return _FakeResponse()


class NotifierTests(unittest.TestCase):
    """通知器核心行为。"""

    def test_log_notifier_writes_structured_message_and_redacts_secret(self) -> None:
        """LogNotifier 输出事件并二次脱敏已知密码。"""
        logger = _FakeLogger()
        notifier = LogNotifier(logger=logger, secrets=["topsecret"])
        event = _failure_event()

        notifier.notify(event)

        self.assertEqual(1, len(logger.records))
        level, message = logger.records[0]
        self.assertEqual(logging.WARNING, level)
        self.assertIn("DatabaseBackupFailed", message)
        self.assertNotIn("topsecret", message)
        self.assertIn("***", message)

    def test_log_notifier_uses_info_for_success(self) -> None:
        """成功事件写 INFO，失败事件写 WARNING。"""
        logger = _FakeLogger()
        notifier = LogNotifier(logger=logger)
        notifier.notify(_success_event())
        notifier.notify(BackupRunCompleted(
            run_id="run-001",
            occurred_at=_time(),
            status=RunStatus.SUCCESS,
            exit_code=0,
        ))

        self.assertEqual([logging.INFO, logging.INFO], [level for level, _ in logger.records])

    def test_noop_notifier_does_nothing(self) -> None:
        """NoopNotifier 不产生错误。"""
        NoopNotifier().notify(_success_event())

    def test_create_notifier_uses_log_by_default(self) -> None:
        """log 配置创建 LogNotifier。"""
        config = NotifyConfig(
            enabled=True,
            on_success=False,
            on_failure=True,
            type=NotifyType.LOG,
        )
        self.assertIsInstance(create_notifier(config), LogNotifier)

    def test_create_notifier_requires_explicit_reserved_channel(self) -> None:
        """smtp/webhook 未注入具体配置时应明确报错。"""
        smtp_config = NotifyConfig(enabled=True, on_success=False, on_failure=True, type=NotifyType.SMTP)
        webhook_config = NotifyConfig(enabled=True, on_success=False, on_failure=True, type=NotifyType.WEBHOOK)
        with self.assertRaises(NotifierConfigError):
            create_notifier(smtp_config)
        with self.assertRaises(NotifierConfigError):
            create_notifier(webhook_config)

    def test_smtp_notifier_captures_transport_and_payload(self) -> None:
        """构造并检查实际 SMTP 传输对象。"""
        transport = _FakeSmtp("smtp.qq.com", 465)
        notifier = SmtpNotifier(
            username="backup@qq.com",
            password="secret",
            recipients=("ops@qq.com",),
            transport_factory=lambda host, port: transport,
        )
        notifier.notify(_failure_event())
        self.assertTrue(transport.login_called)
        self.assertEqual("backup@qq.com", transport.username)
        self.assertEqual("secret", transport.password)
        self.assertTrue(transport.quit_called)
        self.assertEqual(1, len(transport.messages))
        body = transport.messages[0].get_content()
        self.assertIn("DatabaseBackupFailed", body)

    def test_smtp_notifier_swallows_transport_failure(self) -> None:
        """邮件服务器失败只应记录日志，不向调用方抛异常。"""
        class BrokenTransport(_FakeSmtp):
            def send_message(self, message):
                raise OSError("smtp down")

        notifier = SmtpNotifier(
            username="u",
            password="p",
            recipients=("ops@qq.com",),
            transport_factory=lambda host, port: BrokenTransport(host, port),
        )
        # 不应抛出；内部通过 logging 记 warning。
        with self.assertLogs("mysql_backup", level="WARNING"):
            notifier.notify(_failure_event())

    def test_webhook_notifier_posts_json(self) -> None:
        """Webhook 发送事件 JSON。"""
        opener = _FakeOpener()
        notifier = WebhookNotifier("https://example.test/hook", urlopen_factory=opener)
        event = _failure_event("topsecret")
        notifier.notify(event)

        self.assertEqual(1, len(opener.requests))
        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("DatabaseBackupFailed", payload["event"]["type"])
        self.assertEqual("run-001", payload["event"]["run_id"])
        self.assertEqual(10.0, timeout)
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_webhook_notifier_swallows_http_error(self) -> None:
        """Webhook HTTP 错误不应影响调用方。"""
        class BrokenOpener:
            def __call__(self, request, timeout=None):
                raise OSError("network down")

        notifier = WebhookNotifier("https://example.test/hook", urlopen_factory=BrokenOpener())
        # 不应抛出。
        with self.assertLogs("mysql_backup", level="WARNING"):
            notifier.notify(_failure_event())


if __name__ == "__main__":
    unittest.main()