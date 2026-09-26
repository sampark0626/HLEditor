"""XbotGo 분할 파트를 경기 단위로 묶는다 (순수 함수 — I/O 없음).

XbotGo는 녹화가 30분을 넘으면 파일을 잘라 저장한다(앞 파트 30:00 + 나머지).
폰에서 Drive로 올라온 파일들을 녹화 시작 순으로 정렬한 뒤, "30분에 가까운 파일 바로
뒤에 이어서 시작한 파일"을 같은 경기로 잇는다. 마지막 파트가 30분보다 짧으면 경기가
끝난 것으로 보고, 30분에 가까우면 다음 파트를 기다린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

SPLIT_MIN_SEC = 1770.0          # 이 길이 이상인 파일은 "잘린 앞 파트"일 수 있다 (29.5분)
CHAIN_TOLERANCE_SEC = 90.0      # 다음 파트 시작 = 앞 파트 시작 + 앞 파트 길이 ± 이 값
SETTLE_SEC = 180.0              # 경기의 마지막 파일이 도착한 뒤 이만큼 조용해야 처리한다
WAIT_NEXT_PART_SEC = 40 * 60.0  # 다음 파트를 이만큼 기다려도 안 오면 있는 파트만 처리한다


@dataclass(frozen=True)
class FileMeta:
    id: str
    name: str
    duration: float
    start_utc: datetime | None   # 녹화 시작 시각(creation_time, aware UTC). 모르면 None
    arrived_utc: datetime        # 저장소 도착 시각(aware UTC)
    source: str = "inbox"        # "inbox" | "inbox_2d"


@dataclass
class Group:
    files: list[FileMeta]
    timed_out: bool = False      # 다음 파트를 기다리다 시간이 지나 도착한 파트만 처리하는 경우

    @property
    def duration(self) -> float:
        return sum(f.duration for f in self.files)

    @property
    def start_utc(self) -> datetime | None:
        return self.files[0].start_utc

    @property
    def last_arrival(self) -> datetime:
        return max(f.arrived_utc for f in self.files)

    @property
    def wants_2d(self) -> bool:
        return any(f.source == "inbox_2d" for f in self.files)


def natural_key(name: str) -> list:
    """파일명 자연 정렬 키: IMG_2 < IMG_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _sort_key(f: FileMeta):
    return (f.start_utc or f.arrived_utc, natural_key(f.name))


def _is_continuation(prev: FileMeta, nxt: FileMeta, tolerance_sec: float) -> bool:
    if prev.start_utc and nxt.start_utc:
        expected = prev.start_utc + timedelta(seconds=prev.duration)
        return abs((nxt.start_utc - expected).total_seconds()) <= tolerance_sec
    return True  # 녹화 시각을 모르면 정렬 순서만으로 잇는다


def chain_parts(files: list[FileMeta], split_min_sec: float = SPLIT_MIN_SEC,
                tolerance_sec: float = CHAIN_TOLERANCE_SEC) -> list[list[FileMeta]]:
    """녹화 순으로 정렬해 앞 파트(30분에 가까운 파일) 뒤에 이어지는 파일을 한 묶음으로 잇는다."""
    chains: list[list[FileMeta]] = []
    for f in sorted(files, key=_sort_key):
        if chains:
            prev = chains[-1][-1]
            if prev.duration >= split_min_sec and _is_continuation(prev, f, tolerance_sec):
                chains[-1].append(f)
                continue
        chains.append([f])
    return chains


def group_parts(files: list[FileMeta], now: datetime, *,
                split_min_sec: float = SPLIT_MIN_SEC,
                tolerance_sec: float = CHAIN_TOLERANCE_SEC,
                settle_sec: float = SETTLE_SEC,
                wait_next_part_sec: float = WAIT_NEXT_PART_SEC) -> tuple[list[Group], list[Group]]:
    """파일들을 경기로 묶어 (지금 처리할 경기, 더 기다릴 경기)로 나눈다.

    - 마지막 파트가 30분에 가까우면 다음 파트를 기다린다. 단, 녹화 시각상 더 늦게 시작한
      다른 녹화가 이미 있으면 이 경기는 거기서 끝난 것이다(다음 파트가 올 수 없음).
    - 기다린 시간이 wait_next_part_sec를 넘으면 있는 파트만으로 처리한다(timed_out=True).
    - 완결된 경기도 마지막 도착 후 settle_sec가 지나야 처리한다 — 같이 올린 파트가
      순서가 뒤바뀌어 도착하는 경우(짧은 뒤 파트가 먼저 도착) 대비.
    """
    chains = chain_parts(files, split_min_sec, tolerance_sec)
    ready: list[Group] = []
    waiting: list[Group] = []
    for i, parts in enumerate(chains):
        group = Group(parts)
        last = parts[-1]
        quiet_sec = (now - group.last_arrival).total_seconds()
        later_recording = (i + 1 < len(chains) and last.start_utc is not None
                           and chains[i + 1][0].start_utc is not None)
        if last.duration >= split_min_sec and not later_recording:
            if quiet_sec >= wait_next_part_sec:
                group.timed_out = True
                ready.append(group)
            else:
                waiting.append(group)
        elif quiet_sec >= settle_sec:
            ready.append(group)
        else:
            waiting.append(group)
    return ready, waiting
