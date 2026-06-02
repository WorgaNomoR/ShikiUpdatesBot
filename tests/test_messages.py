import random

from main import h, build_message


# =========================
# helper for стабильности
# =========================

def fixed_choice(seq):
    return seq[0]


# =========================
# h()
# =========================

def test_h_escapes_angle_brackets():
    assert h("<Ergo Proxy>") == "&lt;Ergo Proxy&gt;"


def test_h_escapes_ampersand():
    assert h("A&B") == "A&amp;B"


def test_h_escapes_quotes():
    assert h('"test"') == "&quot;test&quot;"


def test_h_plain_text():
    assert h("Evangelion") == "Evangelion"


# =========================
# build_message
# =========================

def test_build_completed_without_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Ergo Proxy",
        event_type="completed",
        score=None,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "Ergo Proxy" in msg
    assert "https://example.com" in msg


def test_build_completed_low_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Anime",
        event_type="completed",
        score=3,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "Anime" in msg


def test_build_completed_mid_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Anime",
        event_type="completed",
        score=5,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "Anime" in msg


def test_build_completed_high_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Anime",
        event_type="completed",
        score=8,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "Anime" in msg


def test_build_completed_perfect_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Anime",
        event_type="completed",
        score=10,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "Anime" in msg


def test_build_score_changed(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="Anime",
        event_type="score_changed",
        score=None,
        old_score=5,
        new_score=8,
        url="https://example.com",
    )

    assert "5" in msg
    assert "8" in msg


def test_build_message_escapes_title(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        title="<Ergo & Proxy>",
        event_type="completed",
        score=8,
        old_score=None,
        new_score=None,
        url="https://example.com",
    )

    assert "&lt;Ergo &amp; Proxy&gt;" in msg
