"""추론 디바이스 자동 감지 (CUDA -> Intel XPU -> CPU)."""
from __future__ import annotations


def resolve_device(preference: str = "auto") -> str:
    """torch 디바이스 문자열을 반환. 'auto'면 사용 가능한 최선을 선택.

    이 PC는 NVIDIA GPU가 없고 Intel Arc iGPU만 있으므로 보통 'xpu' 또는 'cpu'.
    """
    if preference and preference != "auto":
        return preference
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    # Intel XPU (torch>=2.5 + Intel GPU 드라이버)
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    return "cpu"
