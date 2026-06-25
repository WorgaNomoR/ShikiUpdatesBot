import json

from main import (
    load_seen_ids,
    load_subscribers,
    save_seen_ids,
    save_subscribers,
)


def test_load_seen_ids_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr("main.SEEN_IDS_FILE", str(tmp_path / "missing.json"))

    assert load_seen_ids() == set()


def test_load_seen_ids_valid_json(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    file.write_text(
        json.dumps({"seen_ids": [1, 2, 3]}),
        encoding="utf-8",
    )

    monkeypatch.setattr("main.SEEN_IDS_FILE", str(file))

    assert load_seen_ids() == {1, 2, 3}


def test_load_seen_ids_corrupted_json(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    file.write_text("{", encoding="utf-8")

    monkeypatch.setattr("main.SEEN_IDS_FILE", str(file))

    assert load_seen_ids() == set()


def test_save_seen_ids(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("main.SEEN_IDS_FILE", str(file))

    save_seen_ids({1, 2, 3})

    data = json.loads(file.read_text(encoding="utf-8"))

    assert set(data["seen_ids"]) == {1, 2, 3}


def test_seen_ids_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("main.SEEN_IDS_FILE", str(file))

    original = {10, 20, 30}

    save_seen_ids(original)

    loaded = load_seen_ids()

    assert loaded == original


def test_load_subscribers_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr("main.SUBS_FILE", str(tmp_path / "missing.json"))

    assert load_subscribers() == {}


def test_load_subscribers_valid_json(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    file.write_text(
        json.dumps(
            {
                "subscribers": {
                    "123": "Alice",
                    "456": "Bob",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("main.SUBS_FILE", str(file))

    assert load_subscribers() == {
        123: "Alice",
        456: "Bob",
    }


def test_load_subscribers_corrupted_json(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    file.write_text("{", encoding="utf-8")

    monkeypatch.setattr("main.SUBS_FILE", str(file))

    assert load_subscribers() == {}


def test_save_subscribers(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    monkeypatch.setattr("main.SUBS_FILE", str(file))

    save_subscribers(
        {
            123: "Alice",
            456: "Bob",
        }
    )

    data = json.loads(file.read_text(encoding="utf-8"))

    assert data["subscribers"] == {
        "123": "Alice",
        "456": "Bob",
    }


def test_subscribers_roundtrip(monkeypatch, tmp_path):
    file = tmp_path / "subs.json"

    monkeypatch.setattr("main.SUBS_FILE", str(file))

    original = {
        111: "Alice",
        222: "Bob",
    }

    save_subscribers(original)

    loaded = load_subscribers()

    assert loaded == original


def test_save_seen_ids_removes_tmp_file(monkeypatch, tmp_path):
    file = tmp_path / "seen_ids.json"

    monkeypatch.setattr("main.SEEN_IDS_FILE", str(file))

    save_seen_ids({1})

    assert file.exists()
    assert not (tmp_path / "seen_ids.json.tmp").exists()


def test_atomic_write_creates_parent_directory(tmp_path):
    from main import _atomic_write

    target = tmp_path / "nested" / "folder" / "file.json"

    _atomic_write(target, '{"ok": true}')

    assert target.exists()
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_atomic_write_overwrites_existing_file(tmp_path):
    from main import _atomic_write

    target = tmp_path / "data.json"

    target.write_text("old", encoding="utf-8")

    _atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_utcnow_is_naive():
    import main
    dt = main._utcnow()
    assert dt.tzinfo is None
