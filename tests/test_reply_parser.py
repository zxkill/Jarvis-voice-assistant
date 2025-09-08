import pytest

from utils.reply import extract_reply


def test_extract_reply_plain_text():
    assert extract_reply("привет") == "привет"


def test_extract_reply_json():
    assert extract_reply('{"reply": "ок"}') == "ок"


def test_extract_reply_code_block():
    text = """```json\n{\n  \"reply\": \"ура\"\n}\n```"""
    assert extract_reply(text) == "ура"
