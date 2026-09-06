"""파이프라인 전역 설정: 피치 치수, 팀 컬러, 튜닝 파라미터."""
from __future__ import annotations

from dataclasses import dataclass, field

# 표준 축구 피치(cm). roboflow/sports SoccerPitchConfiguration과 동일 스케일.
PITCH_LENGTH_CM = 12000  # 길이(터치라인 방향)
PITCH_WIDTH_CM = 7000    # 폭(골라인 방향)


# BGR 컬러 (OpenCV)
@dataclass(frozen=True)
class Palette:
    field: tuple[int, int, int] = (58, 130, 58)      # 잔디 녹색
    line: tuple[int, int, int] = (245, 245, 245)     # 라인 흰색
    team_a: tuple[int, int, int] = (60, 60, 235)      # 팀 A (빨강 계열)
    team_b: tuple[int, int, int] = (235, 160, 40)     # 팀 B (파랑 계열)
    goalkeeper: tuple[int, int, int] = (40, 220, 220)  # 골키퍼 (노랑)
    referee: tuple[int, int, int] = (20, 20, 20)       # 심판 (검정)
    ball: tuple[int, int, int] = (255, 255, 255)       # 공 (흰색)
    ball_edge: tuple[int, int, int] = (20, 20, 20)


@dataclass
class PipelineConfig:
    # --- 입출력 ---
    device: str = "auto"          # auto | cuda | xpu | cpu
    stride: int = 1               # 프레임 샘플링 (2 = 격프레임 처리)
    target_fps: float | None = None  # None=원본 유지, 예: 30.0

    # --- 검출 ---
    player_conf: float = 0.30
    ball_conf: float = 0.20

    # --- 추적 (ByteTrack) ---
    track_activation_threshold: float = 0.30
    lost_track_buffer: int = 60   # 낮은 앵글 가림 대응 (프레임 수)
    minimum_matching_threshold: float = 0.85

    # --- 팀 분류 ---
    team_fit_stride: int = 30     # 분류기 학습용 크롭 샘플 간격(프레임)
    team_fit_max_crops: int = 800

    # --- 호모그래피 ---
    # 키포인트 신뢰도 하한. 이 구장 실측(2026-08-01): 0.5면 프레임의 75%만
    # 4개 이상 확보되지만 0.3이면 100% 확보되고 평균 개수도 7.6→8.5로 늘어
    # 호모그래피가 더 잘 구속된다.
    keypoint_conf: float = 0.30
    min_keypoints: int = 4        # 호모그래피 재계산 최소 키포인트 수
    homography_reproj_thresh: float = 8.0  # RANSAC 재투영 임계(px)
    # 키포인트 없이 옵티컬플로우 전파/재사용으로 버틸 수 있는 최대 프레임 수.
    # 초과하면 좌표를 만들지 않는다(틀린 좌표보다 없는 편이 낫다).
    homography_max_stale: int = 45

    # --- 스무딩 / 이상치 ---
    max_speed_mps: float = 12.0   # 이 속도 초과 이동은 이상치로 드롭
    savgol_window_s: float = 0.5  # Savitzky-Golay 창(초)
    savgol_polyorder: int = 2
    max_gap_interp_s: float = 1.0 # 이 이하 결측 구간만 보간
    # 피치 밖 허용 여유(피치 크기 대비). 스로인·골키퍼가 라인 밖에 설 수 있어
    # 약간의 여유는 두되, 그 밖은 호모그래피 오류로 보고 버린다.
    pitch_margin_ratio: float = 0.08

    # --- 렌더링 ---
    render_scale: float = 0.10    # cm -> px (0.10 => 12000cm=1200px 길이)
    render_padding: int = 60      # 피치 외곽 여백(px)
    dot_radius: int = 9
    ball_radius: int = 6
    trail_length: int = 6         # FM 잔상 프레임 수
    trail_alpha: float = 0.22
    show_track_id: bool = False

    palette: Palette = field(default_factory=Palette)
