"""ChuuniEvent enum and default Japanese voice lines."""

import random
from enum import Enum


class ChuuniEvent(Enum):
    TASK_START = "task_start"
    CODING = "coding"
    BASH_RUN = "bash_run"
    TEST_PASS = "test_pass"
    TEST_FAIL = "test_fail"
    ERROR = "error"
    TASK_DONE = "task_done"
    PERMISSION_PROMPT = "permission_prompt"


# ---------------------------------------------------------------------------
# Default lines — 3 per event, chuuni style
# ---------------------------------------------------------------------------

LINES: dict[ChuuniEvent, list[str]] = {
    ChuuniEvent.TASK_START: [
        "参る！",
        "いくぞ、全力で！",
        "我が力、解放する時が来た…",
    ],
    ChuuniEvent.CODING: [
        "コードよ…俺の意志に従え！",
        "この指先から、世界を書き換える",
        "フハハ！創造の時だ！",
    ],
    ChuuniEvent.BASH_RUN: [
        "シェルよ、我が命令を刻め！",
        "全システム、起動せよ！",
        "いくぞ…！覚悟しろ！",
    ],
    ChuuniEvent.TEST_PASS: [
        "完璧だ…！全てが意図通りに…！",
        "フハハ！テストは俺の前に跪いた！",
        "この力…本物だった",
    ],
    ChuuniEvent.TEST_FAIL: [
        "くっ…テストに阻まれるとは…",
        "バグよ…お前の存在を許さぬ！",
        "まだだ…まだ終わらぬ！",
    ],
    ChuuniEvent.ERROR: [
        "くっ…予想外の敵か",
        "ぐっ…バグという名の刺客…",
        "この痛み…乗り越えてみせる！",
    ],
    ChuuniEvent.TASK_DONE: [
        "任務完了。世界は救われた",
        "フハハ！完璧だ！",
        "これが…俺の全力だ",
    ],
    ChuuniEvent.PERMISSION_PROMPT: [
        "待機中…",
        "指示を待っている…",
        "我が主よ、命令を…",
    ],
}


def get_line(event: ChuuniEvent) -> str:
    """Return a random Japanese line for *event*."""
    return random.choice(LINES[event])
