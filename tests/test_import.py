import json
from pathlib import Path

import pytest

import main


class DummyUser:
    def __init__(self, user_id):
        self.id = user_id


class DummyDocument:
    def __init__(self, file_name):
        self.file_name = file_name


class DummyBot:
    def __init__(self, content=None):
        self.content = content or ""

    async def download(self, document, destination):
        Path(destination).write_text(self.content, encoding="utf-8")


class DummyMessage:
    def __init__(
        self,
        user_id,
        document=None,
        bot=None,
    ):
        self.from_user = DummyUser(user_id)
        self.document = document
        self.bot = bot or DummyBot()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_import_requires_owner():
    msg = DummyMessage(user_id=999999)

    await main.cmd_import(msg)

    assert "только для владельца" in msg.answers[0].lower()


@pytest.mark.asyncio
async def test_import_without_document(monkeypatch):
    msg = DummyMessage(user_id=main.OWNER_ID)

    await main.cmd_import(msg)

    assert "subscribers.json" in msg.answers[0]


@pytest.mark.asyncio
async def test_import_wrong_extension():
    msg = DummyMessage(
        user_id=main.OWNER_ID,
        document=DummyDocument("file.txt"),
    )

    await main.cmd_import(msg)

    assert ".json" in msg.answers[0]


@pytest.mark.asyncio
async def test_import_invalid_json():
    bot = DummyBot("{broken json")

    msg = DummyMessage(
        user_id=main.OWNER_ID,
        document=DummyDocument("subs.json"),
        bot=bot,
    )

    await main.cmd_import(msg)

    assert "не удалось прочитать" in msg.answers[0].lower()


@pytest.mark.asyncio
async def test_import_empty_subscribers(monkeypatch):
    saved = {}

    def fake_save(subs):
        saved["subs"] = subs

    monkeypatch.setattr(main, "save_subscribers", fake_save)

    bot = DummyBot(json.dumps({}))

    msg = DummyMessage(
        user_id=main.OWNER_ID,
        document=DummyDocument("subs.json"),
        bot=bot,
    )

    await main.cmd_import(msg)

    assert saved["subs"] == {}
    assert "0" in msg.answers[0]


@pytest.mark.asyncio
async def test_import_valid_subscribers(monkeypatch):
    saved = {}

    def fake_save(subs):
        saved["subs"] = subs

    monkeypatch.setattr(main, "save_subscribers", fake_save)

    bot = DummyBot(
        json.dumps(
            {
                "subscribers": {
                    "1": "Alice",
                    "2": "Bob",
                }
            }
        )
    )

    msg = DummyMessage(
        user_id=main.OWNER_ID,
        document=DummyDocument("subs.json"),
        bot=bot,
    )

    await main.cmd_import(msg)

    assert saved["subs"] == {
        1: "Alice",
        2: "Bob",
    }

    assert "2" in msg.answers[0]


@pytest.mark.asyncio
async def test_import_temp_file_removed(monkeypatch, tmp_path):
    temp_files_before = set(tmp_path.glob("*"))

    monkeypatch.setattr(main, "SUBS_FILE", tmp_path / "subs.json")

    bot = DummyBot(
        json.dumps(
            {
                "subscribers": {
                    "1": "Alice"
                }
            }
        )
    )

    msg = DummyMessage(
        user_id=main.OWNER_ID,
        document=DummyDocument("subs.json"),
        bot=bot,
    )

    await main.cmd_import(msg)

    temp_files_after = {
        p.name
        for p in tmp_path.glob("*.import_tmp")
    }

    assert temp_files_after == set()
