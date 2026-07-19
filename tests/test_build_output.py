"""build_output()의 클립 병렬 인코딩이 순서·진행률·취소를 올바르게 지키는지 검증.

실제 ffmpeg 호출(run())은 목으로 교체해 파일만 빠르게 생성한다 — 여기서 검증할
것은 인코딩 품질이 아니라 동시성 관련 로직(완료 순서와 무관한 concat 순서,
progress 콜백, 취소 처리)이다.
"""

import random
import time

import pytest

import soccer_highlights as sh


def _fake_run_factory(delay_by_clip=None):
    """run()을 대체할 목 함수를 만든다. cmd의 출력 파일 경로에 빈 파일을 만든다."""
    def _fake_run(cmd, cwd=None):
        out_path = cmd[-3]  # 모든 run() 호출이 [..., <출력경로>, "-loglevel", "error"]로 끝남
        if delay_by_clip:
            # 파일명(clip000, clip001, ...)에서 인덱스를 뽑아 일부러 완료 순서를 뒤섞는다.
            import re
            m = re.search(r"clip(\d+)", out_path)
            if m:
                idx = int(m.group(1))
                time.sleep(delay_by_clip.get(idx, 0))
        from pathlib import Path
        Path(out_path).write_bytes(b"fake-clip")
        return ""
    return _fake_run


@pytest.fixture
def segments():
    # pre_sec=8, post_sec=5 (총 13초)이므로 20초 간격으로 두면 겹치지 않아 개별 클립이 생성됩니다.
    return [{"peak": float(20 * i)} for i in range(6)]


def test_clips_created_in_filename_order_regardless_of_completion_order(
    tmp_path, monkeypatch, segments
):
    # 짝수 인덱스는 빨리, 홀수 인덱스는 늦게 끝나도록 해 완료 순서를 뒤섞는다.
    delays = {i: (0.0 if i % 2 == 0 else 0.05) for i in range(len(segments))}
    monkeypatch.setattr(sh, "run", _fake_run_factory(delays))

    out_path = tmp_path / "out.mp4"
    sh.build_output("fake_video.mp4", segments, str(out_path), tmp_path, copy_mode=True)

    listfile = (tmp_path / "concat.txt").read_text(encoding="utf-8")
    lines = [line for line in listfile.splitlines() if line.strip()]
    # concat.txt에 나열된 클립 순서가 완료 순서가 아니라 세그먼트 순서(0..5)와 같아야 한다.
    expected_order = [f"clip{i:03d}.mp4" for i in range(len(segments))]
    actual_order = [line.split("'")[1].split("/")[-1] for line in lines]
    assert actual_order == expected_order


def test_progress_callback_reaches_total(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(sh, "run", _fake_run_factory())
    progress_calls = []
    out_path = tmp_path / "out.mp4"
    sh.build_output(
        "fake_video.mp4", segments, str(out_path), tmp_path, copy_mode=True,
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )
    assert len(progress_calls) == len(segments)
    assert progress_calls[-1] == (len(segments), len(segments))
    # done은 매 콜백마다 1씩 늘어야 하며 total을 넘지 않아야 한다
    dones = [d for d, _ in progress_calls]
    assert dones == sorted(dones)
    assert max(dones) == len(segments)


def test_cancellation_stops_before_concat(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(sh, "run", _fake_run_factory())
    call_count = {"n": 0}

    def should_cancel():
        call_count["n"] += 1
        return call_count["n"] >= 2  # 두 번째 확인 시점에 취소 신호

    out_path = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError):
        sh.build_output(
            "fake_video.mp4", segments, str(out_path), tmp_path,
            copy_mode=True, should_cancel=should_cancel,
        )
    assert not out_path.exists()  # concat까지 가지 않았어야 함


def test_single_segment_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "run", _fake_run_factory())
    out_path = tmp_path / "out.mp4"
    sh.build_output("fake_video.mp4", [{"peak": 5.0}], str(out_path), tmp_path, copy_mode=True)
    listfile = (tmp_path / "concat.txt").read_text(encoding="utf-8")
    assert "clip000.mp4" in listfile


def test_overlapping_segments_merged(tmp_path, monkeypatch):
    # 두 구간이 겹치도록 설정 (peak 10s, 14s. pre_sec=8, post_sec=5)
    # 구간 A: 2s ~ 15s
    # 구간 B: 6s ~ 19s
    # 병합 구간: 2s ~ 19s (총 17초짜리 1개 클립이 생성되어야 함)
    monkeypatch.setattr(sh, "run", _fake_run_factory())
    segments = [{"peak": 10.0}, {"peak": 14.0}]
    out_path = tmp_path / "out.mp4"
    
    # timeline 도출 함수 자체 검증
    merged_clips, cand_timestamps = sh.get_merged_timeline(segments, 8.0, 5.0)
    assert len(merged_clips) == 1
    assert merged_clips[0]["start"] == 2.0
    assert merged_clips[0]["end"] == 19.0
    
    # 각 후보의 시작점이 최종 영상 내 어디에 위치하는지 검증
    # peak 10.0 -> segment start 2.0 -> 상대적 시작 0.0
    # peak 14.0 -> segment start 6.0 -> 상대적 시작 4.0
    assert cand_timestamps[id(segments[0])] == 0.0
    assert cand_timestamps[id(segments[1])] == 4.0
    
    # build_output 실행 검증 (1개 클립만 빌드되어야 함)
    sh.build_output(
        "fake_video.mp4", segments, str(out_path), tmp_path,
        copy_mode=True, pre_sec=8.0, post_sec=5.0
    )
    
    listfile = (tmp_path / "concat.txt").read_text(encoding="utf-8")
    lines = [l for l in listfile.splitlines() if l.strip()]
    assert len(lines) == 1
    assert "clip000.mp4" in lines[0]
