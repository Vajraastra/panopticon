"""Pawchive's normal/economic/volume-recovery policy; no UI or storage."""

import math


def resume_count(limit: int, headroom: int) -> int:
    return max(0, limit - max(headroom, math.ceil(limit * .2)))


def update_mode(state: dict, count: int, used: int, limit: int, byte_limit: int, headroom: int) -> str:
    mode = state.get('quota_mode', 'normal')
    if mode not in {'normal', 'economic', 'standby'}:
        mode = 'normal'
    target = resume_count(limit, headroom)
    if count >= limit:
        state['economic_eligible'] = True
    elif count <= target:
        state['economic_eligible'] = False
    if used >= byte_limit:
        mode = 'standby'
    elif mode == 'standby':
        if used <= byte_limit // 2:
            mode = 'normal' if count <= target else 'economic'
    elif mode == 'economic':
        if count <= target:
            mode = 'normal'
    elif state.get('economic_eligible') and used < byte_limit / 2:
        mode = 'economic'
    state['quota_mode'] = mode
    if mode != 'normal':
        state['count_recovery'] = False
    return mode


def release_time(events, used: int, target: int, window: float):
    if used <= target:
        return 0.0
    for at, amount in events:
        used -= amount
        if used <= target:
            return at + window
    return None
