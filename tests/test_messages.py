import random

from main import h, build_message


def fixed_choice(seq):
    return seq[0]


def make_entry(description, title="Ergo Proxy", url="/animes/790-ergo-proxy"):
    return {
        "description": description,
        "target": {
            "name": title,
            "url": url,
        },
        "created_at": "2025-01-01T12:00:00.000Z",
    }


# ==========================================================
# h()
# ==========================================================

def test_h_escapes_angle_brackets():
    assert h("<Ergo Proxy>") == "&lt;Ergo Proxy&gt;"


def test_h_escapes_ampersand():
    assert h("A&B") == "A&amp;B"


def test_h_escapes_quotes():
    assert h('"test"') == "&quot;test&quot;"


def test_h_plain_text():
    assert h("Evangelion") == "Evangelion"


# ==========================================================
# build_message()
# ==========================================================

def test_completed_without_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("просмотрено")
    )

    assert "Ergo Proxy" in msg


def test_completed_low_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 3")
    )

    assert "Ergo Proxy" in msg
    assert "3" in msg


def test_completed_mid_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 5")
    )

    assert "Ergo Proxy" in msg
    assert "5" in msg


def test_completed_high_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 8")
    )

    assert "Ergo Proxy" in msg
    assert "8" in msg


def test_completed_perfect_score(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 10")
    )

    assert "Ergo Proxy" in msg


def test_score_changed(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("изменена оценка с 5 на 8")
    )

    assert "5" in msg
    assert "8" in msg


def test_html_title_escape(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            "<Ergo & Proxy>"
        )
    )

    assert "&lt;Ergo &amp; Proxy&gt;" in msg


# ==========================================================
# links
# ==========================================================

def test_message_contains_shikimori_link(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry("оценено на 8")
    )

    assert "shikimori" in msg.lower()
    assert "ergo-proxy" in msg


def test_message_without_url(monkeypatch):
    monkeypatch.setattr(random, "choice", fixed_choice)

    msg = build_message(
        make_entry(
            "оценено на 8",
            url=""
        )
    )

    assert "Открыть на Shikimori" not in msg
