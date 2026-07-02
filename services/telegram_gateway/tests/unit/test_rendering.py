"""Unit tests for render_approval()."""

from aiogram.types import InlineKeyboardMarkup

from services.telegram_gateway.rendering import render_approval


def test_render_approval_includes_summary_text_and_three_buttons():
    text, keyboard = render_approval({"thread_id": "abc123", "summary": "Ready to merge?"})

    assert text == "Ready to merge?"
    assert isinstance(keyboard, InlineKeyboardMarkup)
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Approve", "Reject", "Edit"]
    assert [button.callback_data for button in buttons] == [
        "approve:abc123",
        "reject:abc123",
        "edit:abc123",
    ]


def test_render_approval_falls_back_to_default_text_when_summary_missing():
    text, _ = render_approval({"thread_id": "abc123"})

    assert text == "Approval needed."
