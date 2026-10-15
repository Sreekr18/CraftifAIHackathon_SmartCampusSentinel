"""Smart Campus Safety Sentinel: CPU-first campus hazard perception and zone sentinel."""
import argparse
import logging
import sys
import time
from pathlib import Path

# Ensure local package imports work reliably
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
from config import SETTINGS
from src.camera import Camera, CameraError
from src.detector import ObjectDetector, DetectorError
from src.event_logger import EventLogger
from src.zone_monitor import Zone, ZoneMonitor
from src.face_hand import FaceHandAnalyzer
from src.motion import motion_metrics
from src.visualizer import Visualizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("sentinel")

def parse_args():
    p = argparse.ArgumentParser(description="Smart Campus Safety Sentinel — Vision & Event System")
    p.add_argument("--source", default=SETTINGS.source, help="Webcam index (0, 1) or path to a test video file")
    p.add_argument("--camera", type=int, default=None, help="Legacy webcam index alias")
    p.add_argument("--model", default=SETTINGS.model_path, help="Path to YOLOv8 weights")
    p.add_argument("--confidence", type=float, default=SETTINGS.confidence_threshold, help="Detection confidence threshold")
    p.add_argument("--inference-width", type=int, default=SETTINGS.inference_width, help="Inference resolution width")
    p.add_argument("--frame-skip", type=int, default=SETTINGS.frame_skip, help="Inference cadence (e.g. 1 = every frame, 2 = alternate)")
    p.add_argument("--cooldown", type=float, default=SETTINGS.event_cooldown_seconds, help="Seconds before re-triggering zone entry")
    p.add_argument("--events", type=Path, default=SETTINGS.log_path, help="Path to append-only JSONL event file")
    p.add_argument("--hands", action="store_true", help="Enable optional MediaPipe hand landmarks (adds CPU compute)")
    p.add_argument("--faces", action="store_true", help="Enable optional MediaPipe face mesh cues")
    p.add_argument("--check-config", action="store_true", help="Validate options without accessing hardware or model")
    return p.parse_args()

def main():
    args = parse_args()
    
    # Handle legacy --camera flag if supplied
    source = str(args.camera) if args.camera is not None else str(args.source)

    if args.frame_skip < 1 or args.inference_width < 160 or not 0 <= args.confidence <= 1 or args.cooldown < 0:
        raise SystemExit("Invalid options: frame-skip >= 1, inference-width >= 160, confidence in [0,1], cooldown >= 0 required.")

    zone = Zone(SETTINGS.zone_x1, SETTINGS.zone_y1, SETTINGS.zone_x2, SETTINGS.zone_y2)

    if args.check_config:
        print("=" * 60)
        print("SMART CAMPUS SENTINEL — CONFIGURATION CHECK")
        print("=" * 60)
        print("Status:           CONFIGURATION VALID")
        print(f"Source:           {source}")
        print(f"Model:            {args.model} (device: cpu, width: {args.inference_width})")
        print(f"Zone Boundary:    x1={zone.x1:.2f}, y1={zone.y1:.2f}, x2={zone.x2:.2f}, y2={zone.y2:.2f}")
        print(f"Event Log:        {args.events.resolve()}")
        print(f"Cooldown:         {args.cooldown}s")
        print("=" * 60)
        return 0

    camera = Camera(source=source)
    detector = ObjectDetector(args.model, args.confidence, "cpu", args.inference_width)
    event_logger = EventLogger(args.events)
    extras = FaceHandAnalyzer(args.hands, args.faces)

    monitors = {}
    previous_centers = {}
    previous_times = {}
    detections = []
    frame_count = 0
    fps = 0.0
    fps_frames = 0
    fps_started = time.perf_counter()
    recent_event_banner = None
    recent_event_time = 0.0

    try:
        LOG.info("Opening input source: '%s'...", source)
        camera.open()
        LOG.info("Video stream established.")
        LOG.info("Loading YOLO model: '%s'...", args.model)
        detector.load()
        LOG.info("Model loaded. Initializing analyzers (zero video retention enabled)...")
        extras.load()

        LOG.info("Sentinel operational. Press 'Q' or 'Esc' in preview window to exit.")
        while True:
            frame = camera.read()
            frame_count += 1
            height, width = frame.shape[:2]
            now = time.perf_counter()

            # Dynamic inference cadence
            if (frame_count - 1) % args.frame_skip == 0:
                detections = detector.detect(frame)

            zone_px = (int(zone.x1 * width), int(zone.y1 * height), int(zone.x2 * width), int(zone.y2 * height))
            people_count = 0
            in_zone_count = 0

            for d in detections:
                x1, y1, x2, y2 = d["bbox"]
                cx, cy = d["center"]
                label_name = d["label"]
                track_id = d.get("track_id")
                is_person = label_name.lower() == "person"

                if is_person:
                    people_count += 1

                inside = is_person and zone.contains(cx / width, cy / height)
                if inside:
                    in_zone_count += 1

                metrics = None
                if track_id is not None:
                    old_center = previous_centers.get(track_id)
                    old_time = previous_times.get(track_id)
                    if old_center is not None and old_time is not None and now > old_time:
                        metrics = motion_metrics(old_center, (cx, cy), now - old_time)
                    previous_centers[track_id] = (cx, cy)
                    previous_times[track_id] = now

                    if is_person:
                        monitor = monitors.setdefault(track_id, ZoneMonitor(zone, args.cooldown))
                        state = monitor.update(cx / width, cy / height, now=now)
                        event_type = state.get("event")

                        if event_type in ("zone_entry", "zone_exit"):
                            recent_event_banner = f"{event_type}: Track ID #{track_id}"
                            recent_event_time = now
                            try:
                                event_logger.log(
                                    event_type=event_type,
                                    confidence=d["confidence"],
                                    bbox_norm=[x1 / width, y1 / height, x2 / width, y2 / height],
                                    track_id=track_id,
                                    object_class=label_name,
                                    direction=(metrics or {}).get("direction", "unknown"),
                                    speed_px_s=(metrics or {}).get("speed_px_s"),
                                )
                                LOG.warning("SAFETY ALERT: %s | Track ID=%s | Center=(%.2f, %.2f)",
                                            event_type.upper(), track_id, cx / width, cy / height)
                            except Exception:
                                LOG.exception("Event logging failed; continuing video feed.")

                Visualizer.draw_detection(
                    frame=frame,
                    bbox=(x1, y1, x2, y2),
                    label=label_name,
                    confidence=d["confidence"],
                    track_id=track_id,
                    is_person=is_person,
                    is_in_zone=inside,
                    motion=metrics,
                )

            # Optional extra landmarks
            extras.process(frame)

            # FPS calculation
            fps_frames += 1
            if now - fps_started >= 1.0:
                fps = fps_frames / (now - fps_started)
                fps_frames = 0
                fps_started = now

            # Expire banner after 3 seconds
            active_banner = recent_event_banner if (now - recent_event_time < 3.0) else None

            # Render tactical HUD
            Visualizer.draw_hud(
                frame=frame,
                zone_coords=zone_px,
                in_zone_count=in_zone_count,
                people_count=people_count,
                object_count=len(detections),
                fps=fps,
                recent_event=active_banner,
            )

            cv2.imshow("Smart Campus Safety Sentinel | Live Perception Feed", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                LOG.info("Exit requested by user.")
                break

    except (CameraError, DetectorError, RuntimeError) as exc:
        LOG.error("Sentinel operational failure: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Execution interrupted via keyboard signal.")
    finally:
        LOG.info("Cleaning up and releasing video/model resources...")
        try:
            extras.close()
        except Exception:
            pass
        camera.release()
        cv2.destroyAllWindows()
        LOG.info("Resources released cleanly.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
