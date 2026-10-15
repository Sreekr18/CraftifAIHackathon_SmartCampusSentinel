"""Central configuration for Smart Campus Safety Sentinel."""
from dataclasses import dataclass
from pathlib import Path
import os

ROOT_DIR = Path(__file__).resolve().parent

def resolve_model_path(preferred_name: str = "yolov8n.pt") -> str:
    """Find local model weights file in build folder or workspace root before web fallback."""
    local_path = ROOT_DIR / preferred_name
    if local_path.is_file():
        return str(local_path)
    parent_path = ROOT_DIR.parent / preferred_name
    if parent_path.is_file():
        return str(parent_path)
    return preferred_name

@dataclass(frozen=True)
class Settings:
    camera_index: int = 0
    source: str = "0"  # Can be webcam index ("0", "1") or video file path ("test.mp4")
    model_path: str = resolve_model_path("yolov8n.pt")
    confidence_threshold: float = 0.40
    inference_width: int = 416
    frame_skip: int = 1  # Run inference every Nth frame; 1 means every frame
    zone_x1: float = 0.25
    zone_y1: float = 0.20
    zone_x2: float = 0.75
    zone_y2: float = 0.80
    event_cooldown_seconds: float = 5.0
    log_path: Path = ROOT_DIR / "data" / "events.jsonl"

SETTINGS = Settings()
