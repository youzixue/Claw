"""Canonical next-session promotion labels shared by training and shadowing."""

from __future__ import annotations


PROMOTION_LABEL_VERSION = "first_board_start_or_consecutive_second_board_v2"


def promotion_event_label(
    *,
    target_board: int,
    outcome_limit_up: bool,
    prediction_day_limit_up: bool,
) -> int:
    """Return the canonical T1/T2 event label.

    T1 is a new first-board start from a non-limit-up prediction session. T2 is
    a consecutive 1-to-2 promotion requiring limit-up events on both sessions.
    Board-height vendor fields are deliberately not required because some feeds
    write consecutive sessions as ``consecutive_days=1``.
    """

    target = int(target_board)
    if target not in {1, 2}:
        raise ValueError("target_board must be 1 or 2")
    if not bool(outcome_limit_up):
        return 0
    return int(not prediction_day_limit_up if target == 1 else prediction_day_limit_up)
