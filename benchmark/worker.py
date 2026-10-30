"""Benchmark a single capturer and write the result as JSON.

Started by ``benchmark.run`` in a fresh interpreter for every library and repetition, so
import costs, singletons and leftover resources of one library never affect another.

    python -m benchmark.worker mss --out result.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil

from .capturers import CAPTURERS, Capturer
from .winapi import OVERLAY_BGR, GameWindow, Overlay, Rect, WindowPrinter, enable_dpi_awareness, find_window

SIMILARITY_THRESHOLD = 0.8  # Correlation above this = the frame shows the game
OVERLAY_THRESHOLD = 0.05  # More overlay pixels than this inside the covered box = the capture saw the overlay
SAMPLE_SIZE = (960, 540)


def frame_signature(frame: np.ndarray) -> int:
    """Cheap fingerprint used to count how many *different* frames a loop received."""
    return hash(frame[::32, ::32].tobytes())


def measure(capturer: Capturer, duration_s: float, fps: float | None = None) -> dict[str, Any]:
    """Grab frames for ``duration_s`` seconds, as fast as possible or paced at ``fps``."""
    process = psutil.Process()
    interval_ns = None if fps is None else round(1e9 / fps)
    latencies_ns: list[int] = []
    new_frames = 0
    last_signature = None

    cpu_start = process.cpu_times()
    start = time.perf_counter_ns()
    end = start + round(duration_s * 1e9)
    next_tick = start
    while (now := time.perf_counter_ns()) < end:
        if interval_ns is not None:
            if now < next_tick:
                time.sleep((next_tick - now) / 1e9)
            # If a grab ran late, start counting again from now instead of bursting to catch up
            next_tick = max(next_tick + interval_ns, time.perf_counter_ns())

        before = time.perf_counter_ns()
        frame = capturer.grab()
        latencies_ns.append(time.perf_counter_ns() - before)

        signature = frame_signature(frame)
        if signature != last_signature:
            new_frames += 1
            last_signature = signature

    wall_s = (time.perf_counter_ns() - start) / 1e9
    cpu_end = process.cpu_times()
    cpu_s = (cpu_end.user - cpu_start.user) + (cpu_end.system - cpu_start.system)
    latencies_ms = np.asarray(latencies_ns) / 1e6
    return {
        "calls": len(latencies_ms),
        "wall_s": wall_s,
        "calls_per_s": len(latencies_ms) / wall_s,
        "new_frames_per_s": new_frames / wall_s,
        "cpu_percent": cpu_s / wall_s * 100,  # 100 % = one logical core fully busy
        "latencies_ms": np.round(latencies_ms, 4).tolist(),
    }


def game_view(frame: np.ndarray, capturer: Capturer) -> np.ndarray:
    """The part of ``frame`` that shows the on-screen game image, whatever the capture source."""
    return frame[capturer.game.visible_client.relative_to(capturer.region).slices()]


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of small grayscale thumbnails: ~1 for the same scene, robust to animation."""
    thumbnails = [cv2.resize(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
                  for x in (a, b)]
    a_flat, b_flat = (t.astype(np.float64).ravel() for t in thumbnails)
    if a_flat.std() == 0 or b_flat.std() == 0:
        return 0.0
    return float(np.corrcoef(a_flat, b_flat)[0, 1])


def overlay_fraction(image: np.ndarray) -> float:
    blue, green, red = (image[..., channel].astype(np.int16) for channel in range(3))
    target_blue, target_green, target_red = OVERLAY_BGR
    close = (abs(blue - target_blue) < 40) & (abs(green - target_green) < 40) & (abs(red - target_red) < 40)
    return float(close.mean())


def save_sample(frame: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail = cv2.resize(frame, SAMPLE_SIZE, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), thumbnail, [cv2.IMWRITE_JPEG_QUALITY, 85])


def check_shows_game(capturer: Capturer, game: GameWindow, sample: Path | None) -> dict[str, Any]:
    """Compare a frame with what PrintWindow sees in the game window at the same moment."""
    printer = WindowPrinter(game.hwnd, game.client.width, game.client.height)
    try:
        reference = printer.grab()
    finally:
        printer.close()
    frame = capturer.grab()
    if sample:
        save_sample(frame, sample)
    reference_view = reference[game.visible_client.relative_to(game.client).slices()]
    score = similarity(game_view(frame, capturer), reference_view)
    return {"similarity": score, "ok": score >= SIMILARITY_THRESHOLD}


def check_covered(capturer: Capturer, game: GameWindow, sample: Path | None) -> dict[str, Any]:
    """Cover the middle of the game with another window and see whether the capture still shows the game."""
    box: Rect = game.visible_client.shrink(0.4)
    with Overlay(box) as overlay:
        overlay.pump(0.3)  # Let the compositor show the overlay
        frame = capturer.grab()
        deadline = time.perf_counter() + 0.3  # Streaming captures need a frame newer than the overlay
        while time.perf_counter() < deadline:
            overlay.pump()
            frame = capturer.grab()
    if sample:
        save_sample(frame, sample)
    fraction = overlay_fraction(frame[box.relative_to(capturer.region).slices()])
    return {"overlay_fraction": fraction, "sees_game": fraction < OVERLAY_THRESHOLD}


def benchmark(key: str, window: str, duration_s: float, agent_fps: float, samples_dir: Path | None) -> dict[str, Any]:
    cls = CAPTURERS[key]
    result: dict[str, Any] = {"key": key}
    game = find_window(window)
    process = psutil.Process()
    rss_before = process.memory_info().rss

    start = time.perf_counter()
    for module in cls.modules:
        importlib.import_module(module)
    result["import_ms"] = (time.perf_counter() - start) * 1000

    capturer = cls(game)
    start = time.perf_counter()
    capturer.open()
    result["init_ms"] = (time.perf_counter() - start) * 1000
    try:
        start = time.perf_counter()
        frame = capturer.grab()
        result["first_frame_ms"] = (time.perf_counter() - start) * 1000

        expected = (capturer.region.height, capturer.region.width, 3)
        if frame.shape != expected or frame.dtype != np.uint8:
            raise ValueError(f"Quadro {frame.shape} {frame.dtype}, esperado {expected} uint8")
        result["frame_shape"] = list(frame.shape)

        for _ in range(5):  # Warm-up: caches, lazy allocations, first GPU copies
            capturer.grab()
        result["max_speed"] = measure(capturer, duration_s)
        result["agent"] = measure(capturer, duration_s, fps=agent_fps)
        result["rss_mb"] = (process.memory_info().rss - rss_before) / 2**20

        result["shows_game"] = check_shows_game(capturer, game, samples_dir and samples_dir / f"{key}.jpg")
        result["covered"] = check_covered(capturer, game, samples_dir and samples_dir / f"{key}-coberto.jpg")
    finally:
        capturer.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key", choices=sorted(CAPTURERS))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window", default="Resident Evil 4")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--agent-fps", type=float, default=30.0)
    parser.add_argument("--samples-dir", type=Path)
    parser.add_argument("--opencv-threads", type=int, default=1,
                        help="threads do OpenCV; -1 = padrão (o pool ocioso gira e infla a medição de CPU)")
    args = parser.parse_args()

    if args.opencv_threads >= 0:
        cv2.setNumThreads(args.opencv_threads)
    enable_dpi_awareness()
    try:
        result = benchmark(args.key, args.window, args.duration, args.agent_fps, args.samples_dir)
    except Exception as error:  # Reported in the results instead of aborting the whole benchmark
        result = {"key": args.key, "error": f"{type(error).__name__}: {error}"}
    args.out.write_text(json.dumps(result), encoding="utf-8")
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
