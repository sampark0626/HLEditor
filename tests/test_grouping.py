"""kaggle_runner.grouping — XbotGo 분할 파트를 경기로 묶는 규칙."""

from datetime import UTC, datetime, timedelta

from kaggle_runner.grouping import FileMeta, chain_parts, group_parts, natural_key

T0 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)        # 녹화 시작(12:00 KST)
LATE = T0 + timedelta(hours=3)                       # 모든 파일이 충분히 오래전에 도착한 시점


def f(name, duration, start_min=None, arrived_min=0, source="inbox"):
    start = T0 + timedelta(minutes=start_min) if start_min is not None else None
    return FileMeta(id=name, name=name, duration=duration, start_utc=start,
                    arrived_utc=T0 + timedelta(minutes=60 + arrived_min), source=source)


def names(groups):
    return [[x.name for x in g.files] for g in groups]


def test_single_short_game_is_ready():
    ready, waiting = group_parts([f("a", 1500, 0)], LATE)
    assert names(ready) == [["a"]] and waiting == []
    assert not ready[0].timed_out


def test_recent_arrival_waits_to_settle():
    now = T0 + timedelta(minutes=61)            # 도착 1분 뒤
    ready, waiting = group_parts([f("a", 1500, 0)], now, settle_sec=180)
    assert ready == [] and names(waiting) == [["a"]]


def test_two_parts_chain_by_creation_time():
    ready, _ = group_parts([f("p1", 1800, 0), f("p2", 700, 30)], LATE)
    assert names(ready) == [["p1", "p2"]]
    assert ready[0].duration == 2500


def test_parts_arriving_out_of_order_are_sorted_by_recording_time():
    ready, _ = group_parts([f("p2", 700, 30, arrived_min=0), f("p1", 1800, 0, arrived_min=5)], LATE)
    assert names(ready) == [["p1", "p2"]]


def test_without_creation_time_chain_by_name_order():
    parts = [f("IMG_0010.MP4", 400), f("IMG_0009.MP4", 1800)]
    parts = [FileMeta(**{**p.__dict__, "arrived_utc": T0}) for p in parts]   # 같은 시각 도착
    ready, _ = group_parts(parts, LATE)
    assert names(ready) == [["IMG_0009.MP4", "IMG_0010.MP4"]]


def test_three_parts_chain():
    ready, _ = group_parts([f("p1", 1800, 0), f("p2", 1800, 30), f("p3", 600, 60)], LATE)
    assert names(ready) == [["p1", "p2", "p3"]]


def test_head_waits_for_next_part_then_times_out():
    head = f("p1", 1800, 0)
    early = head.arrived_utc + timedelta(minutes=5)
    ready, waiting = group_parts([head], early, wait_next_part_sec=40 * 60)
    assert ready == [] and names(waiting) == [["p1"]]

    later = head.arrived_utc + timedelta(minutes=41)
    ready, waiting = group_parts([head], later, wait_next_part_sec=40 * 60)
    assert names(ready) == [["p1"]] and ready[0].timed_out and waiting == []


def test_head_is_complete_when_a_later_separate_recording_exists():
    # 29:50에 녹화를 멈춘 경기 뒤에 다른 경기가 있으면, 앞 경기는 다음 파트가 올 수 없다
    ready, waiting = group_parts([f("g1", 1790, 0), f("g2", 1500, 120)], LATE)
    assert names(ready) == [["g1"], ["g2"]] and waiting == []
    assert not ready[0].timed_out


def test_two_games_are_kept_apart():
    files = [f("a1", 1800, 0), f("a2", 300, 30), f("b1", 1500, 60)]
    ready, _ = group_parts(files, LATE)
    assert names(ready) == [["a1", "a2"], ["b1"]]


def test_gap_beyond_tolerance_breaks_the_chain():
    chains = chain_parts([f("p1", 1800, 0), f("p2", 600, 35)])   # 5분 공백 > 허용 90초
    assert [[x.name for x in c] for c in chains] == [["p1"], ["p2"]]


def test_wants_2d_if_any_part_came_from_2d_inbox():
    ready, _ = group_parts([f("p1", 1800, 0, source="inbox_2d"), f("p2", 600, 30)], LATE)
    assert ready[0].wants_2d


def test_natural_key_orders_numbers():
    assert sorted(["IMG_10", "IMG_9", "img_100"], key=natural_key) == ["IMG_9", "IMG_10", "img_100"]
