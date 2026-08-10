# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Информационная проверка релизного EXE через публичный API VirusTotal."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp

VIRUSTOTAL_API_URL = "https://www.virustotal.com/api/v3"
VIRUSTOTAL_REPORT_URL = "https://www.virustotal.com/gui/file"
LARGE_FILE_THRESHOLD = 32 * 1024 * 1024
POLL_INTERVAL = 30.0
POLL_TIMEOUT = 10 * 60.0
HTTP_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 5 * 60.0
_FLAGGED_CATEGORIES = frozenset({"malicious", "suspicious"})
_REPORT_START_MARKER = "<!-- shikiupdatesbot-security-report:start -->"
_REPORT_END_MARKER = "<!-- shikiupdatesbot-security-report:end -->"


class VirusTotalError(Exception):
    """Ожидаемая безопасная деградация интеграции VirusTotal."""


@dataclass(frozen=True)
class Detection:
    """Одно антивирусное срабатывание."""

    engine: str
    verdict: str


@dataclass(frozen=True)
class ScanReport:
    """Нормализованный результат для workflow и release notes."""

    sha256: str | None
    available: bool
    detections: tuple[Detection, ...] = ()
    total_engines: int = 0
    reason: str | None = None

    @property
    def report_url(self) -> str | None:
        if not self.available or not self.sha256:
            return None
        return f"{VIRUSTOTAL_REPORT_URL}/{self.sha256}"


def _sha256(path: Path) -> str:
    """Посчитать SHA-256 потоково, не загружая EXE целиком в память."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unavailable(sha256: str | None, reason: str) -> ScanReport:
    return ScanReport(sha256=sha256, available=False, reason=reason)


def _data_object(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise VirusTotalError("VirusTotal вернул неожиданный ответ")
    return data


def parse_analysis(payload: dict, sha256: str) -> ScanReport:
    """Нормализовать File или Analysis object из API v3."""
    data = _data_object(payload)
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise VirusTotalError("VirusTotal вернул неожиданный ответ")
    results = attributes.get("last_analysis_results")
    if results is None:
        results = attributes.get("results")
    if not isinstance(results, dict) or not results:
        raise VirusTotalError("VirusTotal не вернул результаты движков")

    detections: list[Detection] = []
    valid_engines = 0
    for engine_id, item in results.items():
        if not isinstance(engine_id, str) or not isinstance(item, dict):
            continue
        engine = item.get("engine_name")
        category = item.get("category")
        verdict = item.get("result")
        if not isinstance(engine, str) or not engine or not isinstance(category, str):
            continue
        if verdict is not None and not isinstance(verdict, str):
            verdict = None
        valid_engines += 1
        if category in _FLAGGED_CATEGORIES:
            detections.append(Detection(engine=engine, verdict=verdict or category))

    if valid_engines == 0:
        raise VirusTotalError("VirusTotal не вернул пригодные результаты движков")
    detections.sort(key=lambda item: (item.engine.casefold(), item.verdict.casefold()))
    return ScanReport(
        sha256=sha256,
        available=True,
        detections=tuple(detections),
        total_engines=valid_engines,
    )


def _http_reason(status: int) -> str:
    if status == 401:
        return "VirusTotal отклонил API-ключ"
    if status == 429:
        return "VirusTotal временно ограничил запросы"
    return f"VirusTotal API временно недоступен (HTTP {status})"


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    api_key: str,
    *,
    data=None,
    allow_not_found: bool = False,
    timeout_total: float = HTTP_TIMEOUT,
) -> dict | None:
    headers = {"x-apikey": api_key}
    timeout = aiohttp.ClientTimeout(total=timeout_total)
    async with session.request(
        method,
        url,
        headers=headers,
        data=data,
        timeout=timeout,
        allow_redirects=False,
    ) as response:
        if allow_not_found and response.status == 404:
            return None
        if not 200 <= response.status < 300:
            raise VirusTotalError(_http_reason(response.status))
        try:
            payload = await response.json()
        except (TypeError, ValueError) as error:
            raise VirusTotalError("VirusTotal вернул невалидный JSON") from error
    if not isinstance(payload, dict):
        raise VirusTotalError("VirusTotal вернул неожиданный ответ")
    return payload


def _analysis_id(payload: dict) -> str:
    analysis_id = _data_object(payload).get("id")
    if not isinstance(analysis_id, str) or not analysis_id:
        raise VirusTotalError("VirusTotal не вернул идентификатор анализа")
    return analysis_id


def _analysis_status(payload: dict) -> str:
    attributes = _data_object(payload).get("attributes")
    if not isinstance(attributes, dict) or not isinstance(attributes.get("status"), str):
        raise VirusTotalError("VirusTotal не вернул статус анализа")
    return attributes["status"]


def _safe_upload_url(value) -> str:
    """Принять одноразовый URL VirusTotal и запретить утечку ключа на иной хост."""
    if not isinstance(value, str):
        raise VirusTotalError("VirusTotal не вернул безопасный URL загрузки")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
    except ValueError as error:
        raise VirusTotalError("VirusTotal не вернул безопасный URL загрузки") from error
    if hostname != "virustotal.com" and not hostname.endswith(".virustotal.com"):
        raise VirusTotalError("VirusTotal не вернул безопасный URL загрузки")
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise VirusTotalError("VirusTotal не вернул безопасный URL загрузки")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urlunsplit(parsed)


async def _upload_file(
    session: aiohttp.ClientSession,
    path: Path,
    api_key: str,
) -> str:
    upload_url = f"{VIRUSTOTAL_API_URL}/files"
    if path.stat().st_size >= LARGE_FILE_THRESHOLD:
        payload = await _request_json(
            session,
            "GET",
            f"{VIRUSTOTAL_API_URL}/files/upload_url",
            api_key,
        )
        assert payload is not None
        upload_url = _safe_upload_url(payload.get("data"))

    with path.open("rb") as stream:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            stream,
            filename=path.name,
            content_type="application/octet-stream",
        )
        payload = await _request_json(
            session,
            "POST",
            upload_url,
            api_key,
            data=form,
            timeout_total=UPLOAD_TIMEOUT,
        )
    assert payload is not None
    return _analysis_id(payload)


async def scan_file(
    path: Path,
    api_key: str | None,
    *,
    session: aiohttp.ClientSession | None = None,
    poll_interval: float = POLL_INTERVAL,
    poll_timeout: float = POLL_TIMEOUT,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ScanReport:
    """Найти или отправить EXE и дождаться результата без падения workflow."""
    sha256: str | None = None
    own_session = session is None
    sleep = sleep or asyncio.sleep
    monotonic = monotonic or asyncio.get_running_loop().time
    try:
        sha256 = _sha256(path)
        if not api_key or not api_key.strip():
            return _unavailable(sha256, "секрет VIRUSTOTAL_API_KEY не настроен")
        if poll_interval < POLL_INTERVAL:
            return _unavailable(sha256, "интервал опроса несовместим с публичным API")
        if poll_timeout < poll_interval:
            return _unavailable(sha256, "тайм-аут анализа меньше интервала опроса")
        if own_session:
            session = aiohttp.ClientSession()
        assert session is not None

        cached = await _request_json(
            session,
            "GET",
            f"{VIRUSTOTAL_API_URL}/files/{sha256}",
            api_key,
            allow_not_found=True,
        )
        if cached is not None:
            return parse_analysis(cached, sha256)

        analysis_id = await _upload_file(session, path, api_key)
        deadline = monotonic() + poll_timeout
        while monotonic() + poll_interval <= deadline:
            await sleep(poll_interval)
            payload = await _request_json(
                session,
                "GET",
                f"{VIRUSTOTAL_API_URL}/analyses/{analysis_id}",
                api_key,
            )
            assert payload is not None
            status = _analysis_status(payload)
            if status == "completed":
                return parse_analysis(payload, sha256)
            if status not in {"queued", "in-progress"}:
                return _unavailable(sha256, f"VirusTotal завершил анализ со статусом {status}")
        return _unavailable(sha256, "VirusTotal не завершил анализ за отведённое время")
    except VirusTotalError as error:
        return _unavailable(sha256, str(error))
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return _unavailable(sha256, "не удалось связаться с VirusTotal")
    except OSError:
        return _unavailable(sha256, "не удалось прочитать релизный EXE")
    finally:
        if own_session and session is not None:
            await session.close()


def _md_text(value: str) -> str:
    """Экранировать динамический текст для обычного Markdown."""
    for char in "\\`*_[]":
        value = value.replace(char, f"\\{char}")
    return value.replace("\r", " ").replace("\n", " ")


def _md_code(value: str) -> str:
    """Подготовить текст code span, не оставляя способный закрыть его backtick."""
    return value.replace("`", "'").replace("\r", " ").replace("\n", " ")


def build_security_markdown(report: ScanReport) -> str:
    """Собрать общий русский блок для summary и GitHub Release."""
    lines = [
        _REPORT_START_MARKER,
        "### 🛡️ Проверка безопасности",
        "",
        "Исходный код ShikiUpdatesBot открыт и не содержит намеренно вредоносной",
        "логики. Windows-версия автоматически собрана из него с помощью публичного",
        "workflow GitHub Actions.",
        "",
        "Для прозрачности готовый EXE также проверяется антивирусными движками",
        "VirusTotal. У неподписанных PyInstaller-сборок возможны отдельные",
        "ложноположительные срабатывания.",
        "",
    ]
    if report.available:
        if report.detections:
            lines.append(
                f"- **Результат:** {len(report.detections)} из {report.total_engines} "
                "движков сообщили о срабатывании"
            )
            rendered = "; ".join(
                f"{_md_text(item.engine)} — `{_md_code(item.verdict)}`"
                for item in report.detections
            )
            lines.append(f"- **Срабатывания:** {rendered}")
        else:
            lines.append(
                f"- **Результат:** ни один из {report.total_engines} движков "
                "не сообщил о срабатывании"
            )
        lines.append(f"- **SHA-256:** `{report.sha256}`")
        lines.append(f"- **Полный отчёт:** [VirusTotal]({report.report_url})")
    else:
        lines.append("- **Результат:** автоматический анализ недоступен")
        lines.append(f"- **Причина:** {_md_text(report.reason or 'неизвестна')}")
        if report.sha256:
            lines.append(f"- **SHA-256:** `{report.sha256}`")
    lines.extend(
        [
            "",
            "Исходники, процесс сборки и результаты проверки доступны для самостоятельной",
            "оценки.",
            _REPORT_END_MARKER,
        ]
    )
    return "\n".join(lines) + "\n"


def action_warning(report: ScanReport) -> str | None:
    """Вернуть безопасный текст warning-аннотации или None для чистого результата."""
    if not report.available:
        return f"Анализ VirusTotal недоступен: {report.reason or 'неизвестная причина'}"
    if report.detections:
        return (
            f"VirusTotal: {len(report.detections)} из {report.total_engines} "
            "движков сообщили о срабатывании"
        )
    return None


def _workflow_command_text(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _print_action_warning(warning: str) -> None:
    """Напечатать warning, сохранив ASCII-fallback для старой Windows-консоли."""
    command = f"::warning::{_workflow_command_text(warning)}"
    try:
        print(command)
    except UnicodeEncodeError:
        print("::warning::VirusTotal warning; see the security report")


def write_report(report: ScanReport, notes_path: Path, summary_path: Path | None) -> None:
    """Записать release notes и дополнить GitHub Actions summary."""
    markdown = build_security_markdown(report)
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(markdown, encoding="utf-8", newline="\n")
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
    warning = action_warning(report)
    if warning:
        _print_action_warning(warning)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Informational VirusTotal release scan")
    parser.add_argument("executable", type=Path)
    parser.add_argument("--notes-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    parser.add_argument("--poll-timeout", type=float, default=POLL_TIMEOUT)
    return parser


def main() -> int:
    """Запустить CLI; любая ошибка даёт честный unavailable-блок и код 0."""
    args = _parser().parse_args()
    try:
        report = asyncio.run(
            scan_file(
                args.executable,
                os.getenv("VIRUSTOTAL_API_KEY"),
                poll_interval=args.poll_interval,
                poll_timeout=args.poll_timeout,
            )
        )
    except Exception:
        sha256 = None
        try:
            sha256 = _sha256(args.executable)
        except OSError:
            pass
        report = _unavailable(sha256, "непредвиденная ошибка интеграции VirusTotal")
    write_report(report, args.notes_path, args.summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
