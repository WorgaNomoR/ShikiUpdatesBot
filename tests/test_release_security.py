# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""VirusTotal release scan: API-контракт, деградация и Markdown."""

import pytest

import release_security


def engine_result(name, category="undetected", result=None):
    return {"engine_name": name, "category": category, "result": result}


def completed_payload(results, *, cached=False):
    key = "last_analysis_results" if cached else "results"
    return {
        "data": {
            "id": "analysis-id",
            "attributes": {"status": "completed", key: results},
        }
    }


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.mark.asyncio
async def test_cached_report_is_used_without_upload(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"cached release")
    payload = completed_payload(
        {
            "clean": engine_result("CleanAV"),
            "flagged": engine_result("Flag AV", "malicious", "Example.Win32"),
        },
        cached=True,
    )
    session = FakeSession(FakeResponse(payload=payload))

    report = await release_security.scan_file(executable, "secret", session=session)

    assert report.available is True
    assert report.total_engines == 2
    assert report.detections == (release_security.Detection("Flag AV", "Example.Win32"),)
    assert len(session.calls) == 1
    assert session.calls[0][:2] == (
        "GET",
        f"{release_security.VIRUSTOTAL_API_URL}/files/{report.sha256}",
    )
    assert session.calls[0][2]["allow_redirects"] is False


def test_parse_analysis_skips_broken_engine_entries():
    payload = completed_payload(
        {
            "clean": engine_result("CleanAV"),
            "missing-category": {"engine_name": "BrokenAV"},
            "not-an-object": None,
            "flagged": engine_result("FlagAV", "malicious", result=123),
        },
        cached=True,
    )

    report = release_security.parse_analysis(payload, "a" * 64)

    assert report.available is True
    assert report.total_engines == 2
    assert report.detections == (release_security.Detection("FlagAV", "malicious"),)


def test_parse_analysis_rejects_report_without_any_valid_engine():
    payload = completed_payload(
        {
            "missing-category": {"engine_name": "BrokenAV"},
            "not-an-object": None,
        },
        cached=True,
    )

    with pytest.raises(release_security.VirusTotalError, match="пригодные результаты"):
        release_security.parse_analysis(payload, "a" * 64)


@pytest.mark.asyncio
async def test_api_redirect_is_not_followed_with_secret_header(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"release")
    session = FakeSession(FakeResponse(status=302))

    report = await release_security.scan_file(executable, "secret", session=session)

    assert report.available is False
    assert "HTTP 302" in report.reason
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_missing_report_is_uploaded_and_queued_analysis_is_polled(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"new release")
    queued = {"data": {"id": "analysis-id", "attributes": {"status": "queued"}}}
    complete = completed_payload({"clean": engine_result("CleanAV")})
    session = FakeSession(
        FakeResponse(status=404),
        FakeResponse(payload={"data": {"id": "analysis-id"}}),
        FakeResponse(payload=queued),
        FakeResponse(payload=complete),
    )
    clock = FakeClock()

    report = await release_security.scan_file(
        executable,
        "secret",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.available is True
    assert report.detections == ()
    assert clock.sleeps == [30.0, 30.0]
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET", "GET"]
    assert session.calls[1][1] == f"{release_security.VIRUSTOTAL_API_URL}/files"


@pytest.mark.asyncio
async def test_large_file_uses_one_time_upload_url(tmp_path, monkeypatch):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"large release")
    monkeypatch.setattr(release_security, "LARGE_FILE_THRESHOLD", 1)
    upload_url = "https://upload.virustotal.com/one-time"
    session = FakeSession(
        FakeResponse(status=404),
        FakeResponse(payload={"data": upload_url}),
        FakeResponse(payload={"data": {"id": "analysis-id"}}),
        FakeResponse(payload=completed_payload({"clean": engine_result("CleanAV")})),
    )
    clock = FakeClock()

    report = await release_security.scan_file(
        executable,
        "secret",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.available is True
    assert [call[:2] for call in session.calls] == [
        ("GET", f"{release_security.VIRUSTOTAL_API_URL}/files/{report.sha256}"),
        ("GET", f"{release_security.VIRUSTOTAL_API_URL}/files/upload_url"),
        ("POST", upload_url),
        ("GET", f"{release_security.VIRUSTOTAL_API_URL}/analyses/analysis-id"),
    ]


def test_official_http_upload_url_is_upgraded_to_https():
    assert release_security._safe_upload_url(
        "http://www.virustotal.com/_ah/upload/one-time"
    ) == "https://www.virustotal.com/_ah/upload/one-time"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/collect",
        "https://virustotal.com.example.test/collect",
        "ftp://www.virustotal.com/collect",
        "https://key@www.virustotal.com/collect",
        "https://[broken/collect",
    ],
)
def test_untrusted_upload_url_is_rejected(url):
    with pytest.raises(release_security.VirusTotalError):
        release_security._safe_upload_url(url)


@pytest.mark.asyncio
async def test_missing_key_does_not_contact_virustotal(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"release")
    session = FakeSession()

    report = await release_security.scan_file(executable, "", session=session)

    assert report.available is False
    assert report.sha256 is not None
    assert "VIRUSTOTAL_API_KEY" in report.reason
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (FakeResponse(status=429), "ограничил запросы"),
        (FakeResponse(payload={"data": None}), "неожиданный ответ"),
        (FakeResponse(payload={"data": {"attributes": {}}}), "результаты движков"),
    ],
)
async def test_api_failures_become_unavailable(tmp_path, response, reason):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"release")

    report = await release_security.scan_file(
        executable,
        "secret",
        session=FakeSession(response),
    )

    assert report.available is False
    assert reason in report.reason


@pytest.mark.asyncio
async def test_queued_analysis_stops_at_bounded_timeout(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"release")
    queued = {"data": {"id": "analysis-id", "attributes": {"status": "queued"}}}
    session = FakeSession(
        FakeResponse(status=404),
        FakeResponse(payload={"data": {"id": "analysis-id"}}),
        FakeResponse(payload=queued),
        FakeResponse(payload=queued),
    )
    clock = FakeClock()

    report = await release_security.scan_file(
        executable,
        "secret",
        session=session,
        poll_timeout=60.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.available is False
    assert "отведённое время" in report.reason
    assert clock.sleeps == [30.0, 30.0]


def test_detected_markdown_contains_exact_verdicts_and_warning():
    report = release_security.ScanReport(
        sha256="a" * 64,
        available=True,
        detections=(
            release_security.Detection("Microsoft", "Trojan:Win32/Wacatac.B!ml"),
            release_security.Detection("Example AV", "Suspicious"),
        ),
        total_engines=71,
    )

    markdown = release_security.build_security_markdown(report)

    assert "2 из 71" in markdown
    assert "Microsoft — `Trojan:Win32/Wacatac.B!ml`" in markdown
    assert "Example AV — `Suspicious`" in markdown
    assert f"https://www.virustotal.com/gui/file/{'a' * 64}" in markdown
    assert "2 из 71" in release_security.action_warning(report)


def test_markdown_uses_separate_escaping_for_engine_and_code_verdict():
    report = release_security.ScanReport(
        sha256="e" * 64,
        available=True,
        detections=(
            release_security.Detection("Engine[One]", "Unsafe[.]`AI"),
        ),
        total_engines=1,
    )

    markdown = release_security.build_security_markdown(report)

    assert "Engine\\[One\\] — `Unsafe[.]'AI`" in markdown
    assert "Unsafe\\[.\\]" not in markdown


def test_clean_and_unavailable_markdown_are_explicit():
    clean = release_security.ScanReport(
        sha256="b" * 64,
        available=True,
        total_engines=70,
    )
    unavailable = release_security.ScanReport(
        sha256="c" * 64,
        available=False,
        reason="секрет не настроен",
    )

    clean_markdown = release_security.build_security_markdown(clean)
    unavailable_markdown = release_security.build_security_markdown(unavailable)

    assert "ни один из 70 движков" in clean_markdown
    assert release_security.action_warning(clean) is None
    assert "автоматический анализ недоступен" in unavailable_markdown
    assert "Полный отчёт" not in unavailable_markdown
    assert "недоступен" in release_security.action_warning(unavailable)


def test_write_report_creates_notes_and_appends_summary(tmp_path, capsys):
    notes = tmp_path / "release" / "security.md"
    summary = tmp_path / "summary.md"
    report = release_security.ScanReport(
        sha256="d" * 64,
        available=False,
        reason="первая строка\nвторая строка",
    )

    release_security.write_report(report, notes, summary)

    assert "автоматический анализ недоступен" in notes.read_text(encoding="utf-8")
    assert summary.read_text(encoding="utf-8") == notes.read_text(encoding="utf-8")
    assert "%0A" in capsys.readouterr().out


def test_sha256_matches_known_value(tmp_path):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"abc")

    assert release_security._sha256(executable) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_main_survives_unexpected_error(tmp_path, monkeypatch):
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"release")
    notes = tmp_path / "release" / "security-notes.md"

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(release_security, "scan_file", boom)
    monkeypatch.setattr(
        "sys.argv",
        ["release_security.py", str(executable), "--notes-path", str(notes)],
    )

    assert release_security.main() == 0
    text = notes.read_text(encoding="utf-8")
    assert "автоматический анализ недоступен" in text
    assert release_security._sha256(executable) in text
