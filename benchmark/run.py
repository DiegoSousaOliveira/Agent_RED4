"""Run the capture benchmark against the live game window, then build the report.

From the project root, with the game open (it can be behind other windows, not minimized):

    python -m benchmark.run                         # every library, 3 repetitions of 5 s
    python -m benchmark.run --repeats 5 --duration 8
    python -m benchmark.run --only mss dxcam printwindow

Each library runs in its own Python process, repetitions interleave the libraries in a
rotating order, and the game window is raised (without taking focus) before each run.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
import winreg
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import psutil

from . import report
from .capturers import CAPTURERS
from .winapi import GameWindow, bring_to_top, enable_dpi_awareness, find_window, primary_display

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_STARTUP_ALLOWANCE_S = 60


def cpu_name() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()


def installed_version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "não instalada"


def describe_environment(game: GameWindow) -> dict[str, Any]:
    gpu, refresh_hz = primary_display()
    distributions = {cls.distribution for cls in CAPTURERS.values() if cls.distribution}
    return {
        "date": datetime.now().isoformat(timespec="seconds"),
        "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
        "python": platform.python_version(),
        "cpu": cpu_name(),
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "gpu": gpu,
        "refresh_hz": refresh_hz,
        "screen": [game.screen.width, game.screen.height],
        "game_title": game.title,
        "game_client": [game.client.width, game.client.height],
        "game_position": [game.client.left, game.client.top],
        "versions": {name: installed_version(name) for name in sorted(distributions | {"numpy", "opencv-python"})},
    }


def run_worker(key: str, args: argparse.Namespace, out: Path, samples_dir: Path) -> dict[str, Any]:
    command = [
        sys.executable, "-m", "benchmark.worker", key,
        "--out", str(out),
        "--window", args.window,
        "--duration", str(args.duration),
        "--agent-fps", str(args.agent_fps),
        "--samples-dir", str(samples_dir),
        "--opencv-threads", str(args.opencv_threads),
    ]
    try:
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                                   timeout=args.duration * 2 + WORKER_STARTUP_ALLOWANCE_S)
    except subprocess.TimeoutExpired:
        return {"key": key, "error": "Tempo esgotado"}
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    # No JSON means the process died in native code (e.g. an access violation inside the library)
    stderr_tail = completed.stderr.strip().splitlines()[-1:] or ["sem saída de erro"]
    return {"key": key, "error": f"O processo morreu (código {completed.returncode}): {stderr_tail[0]}"}


def progress_line(result: dict[str, Any]) -> str:
    label = CAPTURERS[result["key"]].label
    if "error" in result:
        return f"{label:<36} ERRO: {result['error']}"
    speed = result["max_speed"]
    median = sorted(speed["latencies_ms"])[len(speed["latencies_ms"]) // 2]
    return (f"{label:<36} {median:7.2f} ms/foto  {speed['new_frames_per_s']:5.1f} quadros novos/s  "
            f"CPU a {result['agent']['calls_per_s']:.0f} FPS: {result['agent']['cpu_percent']:5.1f}%  "
            f"vê o jogo coberto: {'sim' if result['covered']['sees_game'] else 'não'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", default="Resident Evil 4", help="título exato da janela do jogo")
    parser.add_argument("--repeats", type=int, default=3, help="quantas vezes cada biblioteca é medida")
    parser.add_argument("--duration", type=float, default=5.0, help="segundos de cada teste de captura")
    parser.add_argument("--agent-fps", type=float, default=30.0, help="ritmo do cenário 'agente'")
    parser.add_argument("--pause", type=float, default=1.0, help="segundos de descanso entre execuções")
    parser.add_argument("--opencv-threads", type=int, default=1, help="threads do OpenCV nos workers; -1 = padrão")
    parser.add_argument("--only", nargs="+", choices=list(CAPTURERS), help="medir só estas bibliotecas")
    parser.add_argument("--no-raise", action="store_true", help="não trazer o jogo para a frente antes de cada execução")
    args = parser.parse_args()

    enable_dpi_awareness()
    game = find_window(args.window)  # Fail now with a clear message instead of in every worker
    keys = args.only or list(CAPTURERS)
    report.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    samples_dir = report.RESULTS_DIR / "amostras"

    print(f"Jogo: {game.title} ({game.client.width}x{game.client.height}), "
          f"{len(keys)} bibliotecas x {args.repeats} repetições de {args.duration:g} s\n", flush=True)
    environment = describe_environment(game)
    runs: list[dict[str, Any]] = []
    total = len(keys) * args.repeats
    with tempfile.TemporaryDirectory() as temp_dir:
        for repeat in range(args.repeats):
            # Rotate the order so no library always runs first (cold caches) or right after a heavy one
            shift = repeat % len(keys)
            for key in keys[shift:] + keys[:shift]:
                if not args.no_raise:
                    bring_to_top(game.hwnd)
                time.sleep(args.pause)
                result = run_worker(key, args, Path(temp_dir) / f"{key}-{repeat}.json", samples_dir)
                result["repeat"] = repeat
                runs.append(result)
                print(f"[{len(runs):>2}/{total}] {progress_line(result)}", flush=True)

    raw_path = report.RESULTS_DIR / "raw.json"
    settings = {name: getattr(args, name)
                for name in ("window", "repeats", "duration", "agent_fps", "pause", "opencv_threads")}
    raw_path.write_text(json.dumps({"environment": environment, "settings": settings, "runs": runs}), encoding="utf-8")
    print(f"\nDados brutos: {raw_path}")
    print(f"Relatório:    {report.build(raw_path)}")


if __name__ == "__main__":
    main()
