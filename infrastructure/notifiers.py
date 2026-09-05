"""通知适配器：把领域事件发送到日志、SMTP 或 Webhook。

v1 默认使用 LogNotifier（日志兜底），SmtpNotifier/WebhookNotifier 作为
v2 预留通道实现同一 Notifier 端口。所有实现都必须：
- 不把数据库密码/凭据写入日志；
- 通知失败只记日志，不向上抛出影响备份退出码。
"""

# 延迟类型注解。
from __future__ import annotations

# dataclasses 遍历事件字段；enum 取枚举值；json 生成结构化消息。
import dataclasses
import json
import logging
import smtplib
import ssl
import urllib.request
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from typing import Any

# 领域事件与时间值对象。
from domain.events import (
    BackupRunCompleted,
    DatabaseBackupFailed,
    DatabaseBackupSucceeded,
    DiskSpaceLow,
    DomainEvent,
    VerificationFailed,
)
from domain.model.value_objects import BackupTime, DomainError
from infrastructure.logging_utils import LOGGER_NAME, redact


class NotifierConfigError(DomainError):
    """通知配置缺失或通道参数不合法。"""


def event_to_payload(event: DomainEvent) -> dict[str, Any]:
    """把领域事件转换为 JSON 安全的普通字典。"""
    payload: dict[str, Any] = {
        "type": type(event).__name__,
        "run_id": event.run_id,
        "occurred_at": str(event.occurred_at),
    }

    # 遍历除基类公共字段外的全部子类字段。
    for field_info in dataclasses.fields(event):
        if field_info.name in ("run_id", "occurred_at"):
            continue
        value = getattr(event, field_info.name)

        # BackupTime、枚举、领域值对象统一转成文本/枚举值。
        if isinstance(value, BackupTime):
            value = str(value)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = str(value)
        elif hasattr(value, "value"):
            value = value.value
        elif isinstance(value, tuple):
            value = [str(item) for item in value]

        payload[field_info.name] = value
    return payload


def event_to_text(event: DomainEvent) -> str:
    """生成适合日志和邮件正文的结构化文本。"""
    return json.dumps(event_to_payload(event), ensure_ascii=False, default=str)


class NoopNotifier:
    """通知关闭时使用的空实现。"""

    def notify(self, event: DomainEvent) -> None:
        # 配置关闭时不做任何副作用。
        return


class LogNotifier:
    """默认日志通知器：事件 -> 结构化日志。"""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        # 使用统一的备份 logger，保证 run_id 过滤器和轮转配置生效。
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        # 保存需要二次脱敏的敏感值。
        self._secrets = tuple(secrets)

    def notify(self, event: DomainEvent) -> None:
        """把领域事件写入日志；失败类事件使用 WARNING。"""
        # 事件内容本身已脱敏，这里再用调用方注入的 secrets 做兜底。
        message = redact(event_to_text(event), self._secrets)
        self._logger.log(self._level(event), message)

    @staticmethod
    def _level(event: DomainEvent) -> int:
        """普通事件 INFO，失败/风险事件 WARNING。"""
        if isinstance(
            event,
            (DatabaseBackupFailed, VerificationFailed, DiskSpaceLow),
        ):
            return logging.WARNING
        return logging.INFO


class SmtpNotifier:
    """SMTP 邮件通知器（QQ 邮箱等使用 SSL 465 端口）。

    v1 配置仍保持 log，本类作为 v2 通道预留并已完成可注入传输测试。
    """

    def __init__(
        self,
        host: str = "smtp.qq.com",
        port: int = 465,
        username: str = "",
        password: str = "",
        recipients: Sequence[str] = (),
        use_ssl: bool = True,
        transport_factory: Callable[..., Any] | None = None,
    ) -> None:
        # 保存连接与收件人配置。
        self._host = host
        self._port = port
        self._username = username
        self._password = password  # repr=False 的保护由上层配置对象保证。
        self._recipients = tuple(recipients)
        self._use_ssl = use_ssl
        self._transport_factory = transport_factory

    def notify(self, event: DomainEvent) -> None:
        """发送邮件；任何失败都记录日志并吞掉异常。"""
        try:
            if not self._recipients:
                raise NotifierConfigError("SMTP 收件人不能为空")

            # 默认 SMTP_SSL（QQ 邮箱 465），也可注入 fake transport。
            factory = self._transport_factory or (
                smtplib.SMTP_SSL if self._use_ssl else smtplib.SMTP
            )
            transport = factory(self._host, self._port)

            # 建立连接、认证并发送。
            try:
                if not self._use_ssl:
                    transport.starttls()
                if self._username:
                    transport.login(self._username, self._password)
                transport.send_message(self._build_email(event))
            finally:
                try:
                    transport.quit()
                except Exception:
                    # 关闭连接失败不影响主流程。
                    pass
        except Exception as exc:
            # 通知失败只记录日志，不改变备份退出码。
            logging.getLogger(LOGGER_NAME).warning("SMTP 通知失败：%s", exc)

    def _build_email(self, event: DomainEvent) -> EmailMessage:
        """构造明文邮件。"""
        message = EmailMessage()
        message["From"] = self._username
        # 逗号分隔多个收件人，兼容标准地址格式。
        message["To"] = ", ".join(self._recipients)
        # 主题包含事件类型，方便邮件客户端快速归类。
        message["Subject"] = f"[MySQL 备份] {type(event).__name__}"
        message.set_content(event_to_text(event))
        return message


class WebhookNotifier:
    """HTTP Webhook 通知器（v2 预留，已可测试）。

    只使用标准库 urllib，不引入 requests 依赖。
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        urlopen_factory: Callable[..., Any] | None = None,
    ) -> None:
        # 保存目标地址与请求头。
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._urlopen_factory = urlopen_factory

    def notify(self, event: DomainEvent) -> None:
        """POST JSON 到 Webhook；失败只记录日志。"""
        try:
            if not self._url:
                raise NotifierConfigError("Webhook URL 不能为空")

            body = json.dumps(
                {"event": event_to_payload(event)},
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            request = urllib.request.Request(
                self._url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "mysql-backup-notifier/1.0",
                    **self._headers,
                },
                method="POST",
            )

            # 默认使用 urlopen；测试可注入 fake opener。
            opener = self._urlopen_factory or urllib.request.urlopen
            with opener(request, timeout=self._timeout) as response:
                # 读取响应以释放连接；HTTP 错误在 with 外捕获。
                response.read()
        except Exception as exc:
            # 网络/HTTP 失败只记日志，不改变备份退出码。
            logging.getLogger(LOGGER_NAME).warning("Webhook 通知失败：%s", exc)


def create_notifier(
    config,
    *,
    logger: logging.Logger | None = None,
    secrets: Sequence[str] = (),
    smtp: SmtpNotifier | None = None,
    webhook: WebhookNotifier | None = None,
) -> object:
    """按配置创建通知器；v1 只接 LogNotifier/NoopNotifier。"""
    if not config.enabled:
        return NoopNotifier()

    # 预留通道也可以通过显式注入的实例启用，便于 v2 接线。
    if config.type.value == "log" or config.type.value == "LOG":
        return LogNotifier(logger=logger, secrets=secrets)
    if config.type.value == "smtp" or config.type.value == "SMTP":
        if smtp is None:
            raise NotifierConfigError("SMTP 通道尚未配置")
        return smtp
    if config.type.value == "webhook" or config.type.value == "WEBHOOK":
        if webhook is None:
            raise NotifierConfigError("Webhook 通道尚未配置")
        return webhook
    raise NotifierConfigError(f"不支持的通知类型：{config.type}")