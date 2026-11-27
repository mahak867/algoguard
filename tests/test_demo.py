"""The README demo (scripts/demo_week.py) must keep telling the same story."""
from scripts.demo_week import Week


def test_demo_week_story():
    text = "\n".join(t for t, _ in Week().run())
    assert "buy 55 lots  ->  PASS" in text
    assert "3 s later  ->  BLOCK" in text and "Duplicate of an identical alert" in text
    assert "decimal slip)  ->  HOLD" in text and "8x this bot's largest order" in text
    assert "Reject on Telegram  ->  BLOCK" in text
    assert "action: halve_size" in text and "halved from 56" in text
    assert "reached your limit of 300" in text
