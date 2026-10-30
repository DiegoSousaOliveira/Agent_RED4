"""Build the charts and RELATORIO.md from benchmark/results/raw.json.

Runs automatically at the end of ``benchmark.run``; to rebuild only the report:

    python -m benchmark.report

Every number and every conclusion in the report is computed from the data (raw.json and, if
present, the complementary 60 FPS test), so a new run on another machine (or with the game in
another state) produces a coherent report. The only fixed input is CHOSEN_KEY, the option the
project adopted; the report argues for it with the measured numbers.
"""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import matplotlib

matplotlib.use("Agg")  # Render to files only
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from .capturers import CAPTURERS, Capturer, _game_area, _wait_for
from .winapi import WindowPrinter

PACKAGE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = PACKAGE_DIR / "results"
SAMPLES_DIR = RESULTS_DIR / "amostras"
RECORDING_PATH = RESULTS_DIR / "complementar_60fps.json"
REPORT_PATH = PACKAGE_DIR / "RELATORIO.md"

CHOSEN_KEY = "windows-capture-stream"  # The capture the project uses (copied to capturers.py at the root)
CODE_HIDDEN = {"windows-capture-oneshot"}  # Same API as the chosen one, used the way not to

# Light theme of the dataviz reference palette; the two series colors pass its
# color-vision-deficiency validator.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SOURCE_COLORS = {"tela": "#2a78d6", "janela": "#eb6834"}
SOURCE_NAMES = {"tela": "Captura da tela (monitor)", "janela": "Captura da janela do jogo"}

ROW_IN = 0.38  # Height of one bar row; with BAR_HEIGHT the bar stays <= 24 px at 150 dpi
BAR_HEIGHT = 0.42
HEADER_IN = 1.05
FOOTER_IN = 0.5
DPI = 150

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"],
    "font.size": 9.5,
    "savefig.facecolor": SURFACE,
})


def num(value: float, digits: int = 1) -> str:
    """Brazilian number format: 1.234,5"""
    return f"{value:,.{digits}f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def ms(value: float) -> str:
    return num(value, 2 if value < 1 else 1 if value < 100 else 0)


@dataclass(frozen=True)
class Summary:
    key: str
    label: str
    package: str
    source: str
    api: str
    mode: str
    runs: int
    import_ms: float
    init_ms: float
    first_frame_ms: float
    latency_p5: float
    latency_median: float
    latency_p95: float
    calls_per_s: float
    new_fps: float
    cpu_max: float
    agent_rate: float
    agent_cpu: float
    rss_mb: float
    shows_game_runs: int  # Runs whose frame matched the game window
    covered_runs: int  # Runs that still showed the game with another window on top
    sample_shows_game: bool  # Result of the run that saved the sample photos (the last one)
    sample_sees_covered_game: bool

    @property
    def shows_game(self) -> bool:
        return self.shows_game_runs == self.runs

    @property
    def sees_covered_game(self) -> bool:
        return self.covered_runs == self.runs

    @property
    def startup_ms(self) -> float:
        return self.import_ms + self.init_ms + self.first_frame_ms

    @property
    def cpu_ms_per_grab(self) -> float:
        """CPU time the process spends per capture in the paced (agent) scenario."""
        return self.agent_cpu / 100 / self.agent_rate * 1000


def summarize(raw: dict[str, Any]) -> tuple[list[Summary], dict[str, list[str]]]:
    """Merge the repetitions of each capturer. Returns the summaries and the errors per library."""
    runs_by_key: dict[str, list[dict[str, Any]]] = {}
    for run in raw["runs"]:
        runs_by_key.setdefault(run["key"], []).append(run)

    versions = raw["environment"]["versions"]
    summaries: list[Summary] = []
    failures: dict[str, list[str]] = {}
    for key, cls in CAPTURERS.items():
        runs = runs_by_key.get(key, [])
        if errors := [run["error"] for run in runs if "error" in run]:
            failures[cls.label] = errors
        ok = [run for run in runs if "error" not in run]
        if not ok:
            continue
        latencies = np.concatenate([run["max_speed"]["latencies_ms"] for run in ok])
        p5, p50, p95 = np.percentile(latencies, [5, 50, 95])
        last = max(ok, key=lambda run: run["repeat"])
        # Per-run figures use the median across repetitions: one hitch in the game (or a window
        # switched to the front) during a single run should not move the result
        def per_run(test: str, metric: str) -> float:
            return median(run[test][metric] for run in ok)

        summaries.append(Summary(
            key=key,
            label=cls.label,
            package=f"{cls.distribution} {versions.get(cls.distribution, '?')}" if cls.distribution else "nenhum (ctypes)",
            source=cls.source,
            api=cls.api,
            mode=cls.mode,
            runs=len(ok),
            import_ms=median(run["import_ms"] for run in ok),
            init_ms=median(run["init_ms"] for run in ok),
            first_frame_ms=median(run["first_frame_ms"] for run in ok),
            latency_p5=float(p5),
            latency_median=float(p50),
            latency_p95=float(p95),
            calls_per_s=per_run("max_speed", "calls_per_s"),
            new_fps=per_run("max_speed", "new_frames_per_s"),
            cpu_max=per_run("max_speed", "cpu_percent"),
            agent_rate=per_run("agent", "calls_per_s"),
            agent_cpu=per_run("agent", "cpu_percent"),
            rss_mb=median(run["rss_mb"] for run in ok),
            shows_game_runs=sum(run["shows_game"]["ok"] for run in ok),
            covered_runs=sum(run["covered"]["sees_game"] for run in ok),
            sample_shows_game=last["shows_game"]["ok"],
            sample_sees_covered_game=last["covered"]["sees_game"],
        ))
    return summaries, failures


# --------------------------------------------------------------------------- charts


def _new_chart(rows: int, title: str, subtitle: str, sources: set[str]) -> tuple[Figure, Axes]:
    height = rows * ROW_IN + HEADER_IN + FOOTER_IN
    fig, ax = plt.subplots(figsize=(9, height), dpi=DPI)
    fig.subplots_adjust(left=0.31, right=0.96, top=1 - HEADER_IN / height, bottom=FOOTER_IN / height)
    fig.text(0.02, 1 - 0.18 / height, title, fontsize=12.5, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 1 - 0.46 / height, subtitle, fontsize=9.5, color=INK_SECONDARY, va="top")
    handles = [Patch(color=SOURCE_COLORS[source], label=SOURCE_NAMES[source]) for source in SOURCE_COLORS
               if source in sources]
    if len(handles) > 1:
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.012, 1 - 0.68 / height), ncol=len(handles),
                   frameon=False, fontsize=9, labelcolor=INK_SECONDARY, handlelength=1, handleheight=1,
                   columnspacing=1.8)

    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(axis="x", colors=INK_MUTED, labelsize=8.5, length=0, pad=6)
    ax.tick_params(axis="y", colors=INK_SECONDARY, labelsize=9.5, length=0, pad=8)
    ax.grid(axis="x", color=GRIDLINE, linewidth=0.6)
    ax.set_axisbelow(True)
    return fig, ax


def _set_rows(ax: Axes, ordered: list[Summary]) -> None:
    ax.set_yticks(range(len(ordered)), [summary.label for summary in ordered])
    ax.set_ylim(len(ordered) - 0.5, -0.5)  # First item on top


def _save(fig: Figure, path: Path) -> None:
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def latency_chart(summaries: list[Summary], path: Path) -> None:
    ordered = sorted(summaries, key=lambda s: s.latency_median)
    fig, ax = _new_chart(len(ordered), "Latência por captura",
                         "Mediana (ponto) e faixa de 5% a 95% das capturas (linha), em ms, escala logarítmica. "
                         "Menor é melhor.", {s.source for s in ordered})
    for y, summary in enumerate(ordered):
        color = SOURCE_COLORS[summary.source]
        ax.hlines(y, summary.latency_p5, summary.latency_p95, color=color, linewidth=1.6, alpha=0.45)
        ax.scatter(summary.latency_median, y, s=52, color=color, edgecolors=SURFACE, linewidths=1.2, zorder=3)
        ax.text(summary.latency_p95 * 1.15, y, f"{ms(summary.latency_median)} ms", va="center", fontsize=9,
                color=INK_SECONDARY)

    low = min(s.latency_p5 for s in ordered) / 1.6
    high = max(s.latency_p95 for s in ordered) * 4
    ax.set_xscale("log")
    ax.set_xlim(low, high)
    ax.xaxis.set_major_locator(FixedLocator([t for t in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
                                             if low <= t <= high]))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: num(value, 1 if value < 1 else 0)))
    _set_rows(ax, ordered)
    _save(fig, path)


def bar_chart(summaries: list[Summary], value: Callable[[Summary], float], *, title: str, subtitle: str,
              label: Callable[[Summary], str], path: Path, higher_is_better: bool = False,
              reference: tuple[float, str] | None = None) -> None:
    ordered = sorted(summaries, key=value, reverse=higher_is_better)
    values = [value(s) for s in ordered]
    fig, ax = _new_chart(len(ordered), title, subtitle, {s.source for s in ordered})
    ax.barh(range(len(ordered)), values, height=BAR_HEIGHT, color=[SOURCE_COLORS[s.source] for s in ordered])

    top = max(values + ([reference[0]] if reference else []))
    ax.set_xlim(0, top * 1.22)
    for y, (summary, bar_value) in enumerate(zip(ordered, values)):
        ax.text(bar_value + top * 0.012, y, label(summary), va="center", fontsize=9, color=INK_SECONDARY)
    if reference:
        position, text = reference
        ax.axvline(position, color=INK_MUTED, linewidth=0.8, zorder=1)
        ax.text(position, -0.62, text, ha="center", va="bottom", fontsize=8.5, color=INK_MUTED)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda tick, _: num(tick, 0)))
    _set_rows(ax, ordered)
    _save(fig, path)


def contact_sheet(summaries: list[Summary], suffix: str, title: str, caption: Callable[[Summary], str],
                  path: Path) -> bool:
    images = [(s, SAMPLES_DIR / f"{s.key}{suffix}.jpg") for s in summaries]
    images = [(s, image) for s, image in images if image.exists()]
    if not images:
        return False
    columns = 2
    rows = math.ceil(len(images) / columns)
    cell_width = 4.4
    height = rows * (cell_width * 9 / 16 + 0.42) + 0.55
    fig = plt.figure(figsize=(columns * cell_width + 0.3, height), dpi=DPI)
    fig.text(0.015, 1 - 0.15 / height, title, fontsize=12.5, fontweight="bold", color=INK, va="top")
    grid = fig.add_gridspec(rows, columns, left=0.015, right=0.985, top=1 - 0.75 / height, bottom=0.01,
                            hspace=0.22, wspace=0.04)
    for index, (summary, image) in enumerate(images):
        ax = fig.add_subplot(grid[index // columns, index % columns])
        ax.imshow(cv2.cvtColor(cv2.imread(str(image)), cv2.COLOR_BGR2RGB))
        ax.set_axis_off()
        ax.set_title(caption(summary), loc="left", fontsize=9.5, color=INK, pad=4)
    fig.savefig(path, dpi=DPI, pil_kwargs={"quality": 85})
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- markdown


def table(headers: list[str], rows: list[list[str]], align: str) -> str:
    """Markdown table; ``align`` has one char per column: l(eft) or r(ight)."""
    separator = ["---:" if a == "r" else ":---" for a in align]
    lines = [headers, separator, *rows]
    return "\n".join("| " + " | ".join(line) + " |" for line in lines)


def passed(count: int, total: int) -> str:
    icon = "✅" if count == total else "❌" if count == 0 else "⚠️"
    return f"{icon} {count}/{total}"


def names(summaries: list[Summary]) -> str:
    return ", ".join(f"`{s.label}`" for s in summaries) or "nenhuma"


def bullet_list(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


@dataclass(frozen=True)
class RecordingSummary:
    """One option in the complementary 60 FPS (recording) test; medians across its runs."""

    runs: int
    rate: float
    new_fps: float
    new_fps_worst: float
    cpu: float
    latency_median: float
    latency_p95: float

    @property
    def cpu_ms_per_grab(self) -> float:
        return self.cpu / 100 / self.rate * 1000


Recording = tuple[dict[str, Any], dict[str, RecordingSummary]]


def load_recording(path: Path = RECORDING_PATH) -> Recording | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, {
        key: RecordingSummary(
            runs=len(runs),
            rate=median(run["calls_per_s"] for run in runs),
            new_fps=median(run["new_frames_per_s"] for run in runs),
            new_fps_worst=min(run["new_frames_per_s"] for run in runs),
            cpu=median(run["cpu_percent"] for run in runs),
            latency_median=median(run["latency_median_ms"] for run in runs),
            latency_p95=median(run["latency_p95_ms"] for run in runs),
        )
        for key, runs in data["runs"].items()
    }


def decision(summaries: list[Summary], agent_fps: float, refresh_hz: int, recording: Recording | None) -> str:
    """Why the project uses CHOSEN_KEY, argued only with the measured numbers."""
    by_key = {s.key: s for s in summaries}
    chosen = by_key.get(CHOSEN_KEY)
    if chosen is None:
        return f"A opção escolhida (`{CHOSEN_KEY}`) não tem execuções válidas neste benchmark."
    keeps_up = [s for s in summaries if s.agent_rate >= agent_fps * 0.95]
    covered = [s for s in summaries if s.sees_covered_game]
    # The fair rival: also sees the covered game and keeps up with the agent
    rival = min((s for s in covered if s in keeps_up and s is not chosen), key=lambda s: s.agent_cpu, default=None)
    oneshot = by_key.get("windows-capture-oneshot")
    screen = min((s for s in keeps_up if s.source == "tela"), key=lambda s: s.agent_cpu, default=None)
    finalists = [s for s in (chosen, rival, oneshot, screen) if s]
    record_fps = recording[0]["fps"] if recording else None
    record = recording[1] if recording else {}

    def at_record_fps(summary: Summary, show: Callable[[RecordingSummary], str]) -> str:
        return show(record[summary.key]) if summary.key in record else "não medido"

    rows: list[tuple[str, Callable[[Summary], str]]] = [
        ("Captura", lambda s: s.source),
        ("Vê o jogo com outra janela na frente", lambda s: passed(s.covered_runs, s.runs)),
        ("Tempo por foto (mediana)", lambda s: f"{ms(s.latency_median)} ms"),
        ("Quadros novos por segundo", lambda s: num(s.new_fps)),
        (f"CPU a {num(agent_fps, 0)} FPS", lambda s: f"{num(s.agent_cpu)}%"),
    ]
    if record:
        rows += [
            (f"CPU a {record_fps} FPS (gravação)", lambda s: at_record_fps(s, lambda r: f"{num(r.cpu)}%")),
            (f"Tempo por foto a {record_fps} FPS", lambda s: at_record_fps(s, lambda r: f"{ms(r.latency_median)} ms")),
        ]
    rows += [
        ("Memória extra", lambda s: f"{num(s.rss_mb, 0)} MB"),
        ("Até a primeira foto", lambda s: f"{ms(s.startup_ms)} ms"),
    ]
    comparison = table(
        [""] + [f"`{s.label}`" + (" **(escolhida)**" if s is chosen else "") for s in finalists],
        [[f"**{name}**"] + [show(s) for s in finalists] for name, show in rows],
        "l" + "r" * len(finalists),
    )

    reasons: list[str] = []
    screen_captures = [s for s in summaries if s.source == "tela"]
    blind = [s for s in screen_captures if s.covered_runs == 0]
    reason = (f"**Enxerga o jogo mesmo com outra janela na frente.** Só as capturas de janela ({names(covered)}) "
              f"passaram nesse teste em todas as execuções; as {len(blind)} capturas de tela fotografaram a janela que "
              "estava por cima.")
    if missed := [s for s in summaries if not s.shows_game]:
        reason += (f" Durante o próprio benchmark, {names(missed)} chegou a fotografar outra janela que foi para a "
                   "frente do jogo.")
    reasons.append(reason)
    if rival and rival.latency_median > 2 * chosen.latency_median:
        reasons.append(
            f"**Não trava o agente.** Cada foto leva {ms(chosen.latency_median)} ms, contra {ms(rival.latency_median)} ms "
            f"do `{rival.label}` ({num(rival.latency_median / chosen.latency_median, 0)}× mais). O `{rival.label}` espera "
            f"o Windows desenhar a janela a cada chamada; a `{chosen.label}` recebe os quadros numa thread própria e o "
            f"agente só copia o último. A {num(agent_fps, 0)} FPS, o `{rival.label}` deixaria o agente parado "
            f"{num(rival.latency_median / (1000 / agent_fps) * 100, 0)}% do tempo."
        )
    if chosen.new_fps >= refresh_hz * 0.95:
        reasons.append(f"**Acompanha o jogo inteiro.** Recebeu {num(chosen.new_fps)} quadros novos por segundo, o "
                       f"máximo que o monitor de {refresh_hz} Hz mostra.")
    if rival and chosen.key in record and rival.key in record:
        mine, theirs = record[chosen.key], record[rival.key]
        if mine.cpu < theirs.cpu:
            reasons.append(
                f"**É a mais barata para gravar dados de treino a {record_fps} FPS.** {num(mine.cpu)}% de CPU contra "
                f"{num(theirs.cpu)}% do `{rival.label}` ({ms(mine.cpu_ms_per_grab)} contra "
                f"{ms(theirs.cpu_ms_per_grab)} ms de CPU por foto). Nesse ritmo o `{rival.label}` fica "
                f"{ms(theirs.latency_median)} ms parado em cada quadro de {ms(1000 / record_fps)} ms; a "
                f"`{chosen.label}`, {ms(mine.latency_median)} ms."
            )
    if oneshot:
        reason = (f"**Com a sessão aberta, e não uma por foto.** O jeito do `main.py` original (`{oneshot.label}`) levou "
                  f"{ms(oneshot.latency_median)} ms por foto, {num(oneshot.latency_median / chosen.latency_median, 0)}× "
                  f"mais que a sessão contínua, e não passou de {num(oneshot.calls_per_s)} fotos por segundo.")
        if oneshot.rss_mb > 2 * chosen.rss_mb:
            reason += f" A memória ainda cresceu {num(oneshot.rss_mb, 0)} MB, contra {num(chosen.rss_mb, 0)} MB."
        reasons.append(reason)

    costs: list[str] = []
    if rival and chosen.agent_cpu > rival.agent_cpu:
        cost = (f"A {num(agent_fps, 0)} FPS gasta mais CPU que o `{rival.label}` ({num(chosen.agent_cpu)}% contra "
                f"{num(rival.agent_cpu)}%), porque no Windows 10 a sessão recebe todos os quadros do jogo mesmo que o "
                "agente use só parte deles (`minimum_update_interval` não é suportado).")
        if record:
            cost += f" Gravando a {record_fps} FPS, esse trabalho deixa de ser desperdício."
        costs.append(cost)
    if rival and chosen.startup_ms > 2 * rival.startup_ms:
        costs.append(f"Demora {ms(chosen.startup_ms)} ms para entregar a primeira foto ({ms(rival.startup_ms)} ms no "
                     f"`{rival.label}`). Só acontece uma vez, ao abrir a sessão.")
    if rival and chosen.rss_mb > rival.rss_mb:
        costs.append(f"Usa mais memória: {num(chosen.rss_mb, 0)} MB contra {num(rival.rss_mb, 0)} MB.")
    costs.append("No Windows 10 desenha uma borda amarela em volta da janela do jogo enquanto captura.")
    if screen and screen.agent_cpu < chosen.agent_cpu:
        costs.append(f"Se o jogo ficasse sempre na frente, `{screen.label}` gastaria menos CPU "
                     f"({num(screen.agent_cpu)}%). Mas ela fotografa o monitor: qualquer janela, notificação ou a barra "
                     "de tarefas por cima do jogo entraria nas imagens do dataset.")

    rules = [
        "Abra a sessão uma vez e mantenha aberta enquanto o agente roda (é o que o `capturers.py` da raiz faz).",
        "Dentro do `on_frame_arrived`, copie o quadro só com numpy: chamar o OpenCV ali derruba o processo na "
        "próxima sessão de captura.",
        "Chame `cv2.setNumThreads(1)` no início do programa: o pool ocioso do OpenCV quase dobra o uso de CPU "
        "(veja as observações).",
    ]
    if chosen.key in record and record[chosen.key].new_fps_worst < record_fps * 0.95:
        rules.append(
            f"Para gravar dataset, consuma cada quadro no próprio callback (numa fila, junto com `frame.timespan`) em "
            f"vez de ler o último a {record_fps} Hz: lendo o último, uma das execuções a {record_fps} FPS pegou só "
            f"{num(record[chosen.key].new_fps_worst)} quadros diferentes por segundo."
        )
    rules.append("Nunca minimize o jogo. Atrás de outras janelas funciona normalmente.")

    return f"""Comparação das finalistas: a escolhida, a outra captura de janela que aguenta o agente, o jeito
original do `main.py` e a captura de tela mais barata.

{comparison}

### Por que ela

{bullet_list(reasons)}

### O que se paga por ela

{bullet_list(costs)}

### Como usar sem perder desempenho

{bullet_list(rules)}"""


def recording_section(recording: Recording | None) -> str:
    if not recording:
        return ""
    info, record = recording
    fps = info["fps"]
    runs = max(r.runs for r in record.values())
    rows = [
        [f"`{CAPTURERS[key].label}`", num(r.rate), num(r.new_fps), num(r.new_fps_worst), num(r.cpu),
         ms(r.cpu_ms_per_grab), ms(r.latency_median), ms(r.latency_p95)]
        for key, r in sorted(record.items(), key=lambda item: item[1].cpu)
    ]
    return f"""
### Gravação a {fps} FPS (teste complementar)

Para gravar dados de treino, o agente precisa de todos os {fps} quadros por segundo. As duas opções que veem o
jogo coberto e aguentam o agente foram medidas nesse ritmo em
{datetime.fromisoformat(info['date']).strftime('%d/%m/%Y')} ({runs} execuções de cada; mediana entre elas).
{info['description']}

{table(["Opção", "FPS atingido", "Quadros novos/s", "Quadros novos/s (pior execução)", "CPU (%)",
        "CPU por foto (ms)", "Tempo por foto (ms)", "Tempo por foto, 95% (ms)"], rows, "lrrrrrrr")}
"""


def code_section(summaries: list[Summary], agent_fps: float) -> str:
    """The adapter of each option, read from capturers.py itself so it never drifts from what was measured."""
    helpers = [_wait_for, _game_area, WindowPrinter]
    shown = sorted((s for s in summaries if s.key not in CODE_HIDDEN),
                   key=lambda s: (s.key != CHOSEN_KEY, s.agent_cpu))
    parts = [
        "Cada opção é uma classe de `benchmark/capturers.py` que herda de `Capturer`: `open()` cria os recursos uma "
        "vez, `grab()` devolve a imagem e `close()` libera tudo. Abaixo de cada classe vêm as funções auxiliares que "
        "ela usa. A escolhida aparece primeiro e aberta; as outras, da que menos gasta CPU para a que mais gasta. "
        "O código da `windows-capture (1 sessão por foto)` não entra: é a mesma API da escolhida, usada do jeito "
        "que o benchmark mostrou que não se deve usar.",
        f"<details>\n<summary>Classe base <code>Capturer</code></summary>\n\n```python\n"
        f"{inspect.getsource(Capturer).rstrip()}\n```\n\n</details>",
    ]
    for summary in shown:
        source = inspect.getsource(CAPTURERS[summary.key]).rstrip()
        used = [helper for helper in helpers if re.search(rf"\b{helper.__name__}\b", source)]
        code = "\n\n\n".join([source] + [inspect.getsource(helper).rstrip() for helper in used])
        is_chosen = summary.key == CHOSEN_KEY
        facts = (f"{ms(summary.latency_median)} ms por foto · {num(summary.new_fps)} quadros novos/s · "
                 f"CPU a {num(agent_fps, 0)} FPS: {num(summary.agent_cpu)}% · "
                 f"jogo coberto: {passed(summary.covered_runs, summary.runs)}")
        parts.append(
            f"### `{summary.label}`{' (escolhida)' if is_chosen else ''}\n\n{facts}\n\n"
            f"<details{' open' if is_chosen else ''}>\n<summary>Código</summary>\n\n"
            f"```python\n{code}\n```\n\n</details>"
        )
    return "\n\n".join(parts)


def highlights(summaries: list[Summary], refresh_hz: int) -> list[str]:
    by_latency = sorted(summaries, key=lambda s: s.latency_median)
    fastest, slowest = by_latency[0], by_latency[-1]
    # Differences inside 1.5x of the fastest are too small to matter for an agent
    tied = [s for s in by_latency if s.latency_median <= fastest.latency_median * 1.5]
    cheapest = min(summaries, key=lambda s: s.agent_cpu)
    quickest_start = min(summaries, key=lambda s: s.startup_ms)
    if len(tied) > 1:
        speed = (f"**Mais rápidas por captura:** {names(tied)}, todas entre {ms(tied[0].latency_median)} e "
                 f"{ms(tied[-1].latency_median)} ms (mediana).")
    else:
        speed = f"**Mais rápida por captura:** `{fastest.label}`, {ms(fastest.latency_median)} ms (mediana)."
    return [
        f"{speed} A mais lenta, `{slowest.label}`, levou {ms(slowest.latency_median)} ms.",
        f"**Menor uso de CPU no cenário do agente:** `{cheapest.label}`, {num(cheapest.agent_cpu)}% de um núcleo.",
        f"**Receberam todos os quadros do jogo (~{refresh_hz} por segundo):** "
        f"{names([s for s in summaries if s.new_fps >= refresh_hz * 0.95])}.",
        "**Continuam vendo o jogo quando outra janela fica por cima:** "
        f"{names([s for s in summaries if s.sees_covered_game])}. Todas as outras fotografam o que está na frente.",
        f"**Inicialização mais rápida:** `{quickest_start.label}`, {ms(quickest_start.startup_ms)} ms até a primeira foto.",
    ]


def write_markdown(raw: dict[str, Any], summaries: list[Summary], failures: dict[str, list[str]],
                   charts: dict[str, str], recording: Recording | None) -> str:
    env, settings = raw["environment"], raw["settings"]
    agent_fps = settings["agent_fps"]
    opencv_threads = settings.get("opencv_threads", -1)
    opencv_setting = (f"o OpenCV foi limitado a {opencv_threads} thread{'s' if opencv_threads > 1 else ''} em "
                      "todas as execuções (`cv2.setNumThreads`), para que o pool ocioso dele não distorça a CPU "
                      "medida" if opencv_threads >= 0 else "o OpenCV usou o número padrão de threads")
    refresh_hz = env["refresh_hz"]
    when = datetime.fromisoformat(env["date"]).strftime("%d/%m/%Y %H:%M")
    by_latency = sorted(summaries, key=lambda s: s.latency_median)
    chosen_label = CAPTURERS[CHOSEN_KEY].label

    def image(name: str, alt: str) -> str:
        return f"![{alt}]({charts[name]})" if name in charts else ""

    environment_table = table(["Item", "Valor"], [
        ["Sistema", env["os"]],
        ["Processador", f"{env['cpu']} ({env['cores']} núcleos, {env['threads']} threads)"],
        ["Placa de vídeo", env["gpu"]],
        ["Memória", f"{num(env['ram_gb'])} GB"],
        ["Monitor", f"{env['screen'][0]}x{env['screen'][1]} a {refresh_hz} Hz"],
        ["Jogo", f"{env['game_title']}, modo janela, imagem de {env['game_client'][0]}x{env['game_client'][1]} "
                 f"na posição ({env['game_position'][0]}, {env['game_position'][1]})"],
        ["Python", env["python"]],
        ["numpy / OpenCV", f"{env['versions'].get('numpy', '?')} / {env['versions'].get('opencv-python', '?')}"],
    ], "ll")

    libraries_table = table(["Opção", "Pacote", "API do Windows", "Captura", "Como foi usada"], [
        [f"`{s.label}`", s.package, s.api, s.source, s.mode] for s in summaries
    ], "lllll")

    latency_table = table(["Opção", "Mediana (ms)", "5% (ms)", "95% (ms)", "Chamadas/s", "Quadros novos/s"], [
        [f"`{s.label}`", ms(s.latency_median), ms(s.latency_p5), ms(s.latency_p95), num(s.calls_per_s),
         num(s.new_fps)] for s in by_latency
    ], "lrrrrr")

    cpu_table = table(["Opção", f"CPU a {num(agent_fps, 0)} FPS (%)", "FPS atingido", "CPU por foto (ms)",
                       "CPU no máximo (%)", "Memória extra (MB)"], [
        [f"`{s.label}`", num(s.agent_cpu), num(s.agent_rate), ms(s.cpu_ms_per_grab), num(s.cpu_max, 0),
         num(s.rss_mb, 0)] for s in sorted(summaries, key=lambda s: s.agent_cpu)
    ], "lrrrrr")

    startup_table = table(["Opção", "import (ms)", "Inicialização (ms)", "Primeira foto (ms)", "Total (ms)"], [
        [f"`{s.label}`", ms(s.import_ms), ms(s.init_ms), ms(s.first_frame_ms), ms(s.startup_ms)]
        for s in sorted(summaries, key=lambda s: s.startup_ms)
    ], "lrrrr")

    background_table = table(["Opção", "Captura", "Mostrou o jogo (visível)", "Mostrou o jogo (coberto)"], [
        [f"`{s.label}`", s.source, passed(s.shows_game_runs, s.runs), passed(s.covered_runs, s.runs)]
        for s in sorted(summaries, key=lambda s: (-s.covered_runs, -s.shows_game_runs, s.label))
    ], "llll")
    missed = [s for s in summaries if not s.shows_game]
    missed_note = ""
    if missed:
        missed_note = (
            "\n\nQuando uma captura **de tela** não mostra o jogo com ele \"visível\", é porque outra janela foi para "
            "a frente durante aquela execução (aqui, com o computador em uso): "
            + ", ".join(f"`{s.label}` em {s.runs - s.shows_game_runs} de {s.runs}" for s in missed)
            + ". É exatamente o risco de usar captura de tela em um agente."
        )

    failures_section = ""
    if failures:
        failures_section = "\n## Falhas registradas\n\n" + bullet_list(
            [f"`{label}`: " + "; ".join(sorted(set(errors))) for label, errors in failures.items()]) + "\n"

    return f"""# Benchmark de captura de tela: Resident Evil 4 Remake

Comparação das principais bibliotecas Python para capturar a tela do jogo no Windows, medida
com o jogo rodando de verdade. Gerado automaticamente por `python -m benchmark.run` em {when}
({len(summaries)} configurações, {settings['repeats']} repetições de {num(settings['duration'], 0)} s cada).

## Resumo

{bullet_list(highlights(summaries, refresh_hz))}

## Escolha do Agent RED4: `{chosen_label}`

{decision(summaries, agent_fps, refresh_hz, recording)}

## Ambiente

{environment_table}

## O que foi testado

{libraries_table}

Por baixo, são só quatro tecnologias do Windows:

- **GDI BitBlt** (`mss`, `Pillow`, `pyautogui`, `pywin32`): o processador copia os pixels do monitor a
  cada chamada. Simples e universal, mas copiar 1920x1080 pela CPU é caro.
- **DXGI Desktop Duplication** (`dxcam`, `bettercam`): a placa de vídeo entrega a imagem pronta do
  monitor, e só quando ela muda. Muito barato, mas também só vê o que está na tela.
- **Windows Graphics Capture** (`windows-capture`): o compositor do Windows entrega a imagem da
  própria janela do jogo, mesmo coberta por outras. Não funciona com a janela minimizada.
- **PrintWindow com `PW_RENDERFULLCONTENT`**: pede ao Windows o conteúdo atual da janela. Também
  funciona com ela coberta; só precisa de `ctypes` (nenhum pacote extra).

## Metodologia

- **Mesmo contrato para todas:** cada captura devolve um `numpy.ndarray` (altura x largura x 3, BGR,
  `uint8`) que pertence a quem chamou, pronto para o OpenCV. O tempo de conversão para esse formato
  está incluído. As de tela capturam o monitor inteiro ({env['screen'][0]}x{env['screen'][1]}); as de
  janela, a imagem do jogo ({env['game_client'][0]}x{env['game_client'][1]}), a mesma quantidade de pixels.
  Na conversão de cores, {opencv_setting}.
- **Uso correto de cada biblioteca:** objetos, contextos de dispositivo e buffers são criados uma vez e
  reaproveitados. `dxcam`/`bettercam` devolvem o último quadro quando a tela não mudou, que é o que um
  agente precisa (a imagem atual).
- **Isolamento:** cada biblioteca roda em um processo Python novo, então o custo de import, objetos
  globais e recursos de uma não afetam a outra. As repetições alternam a ordem de execução.
- **Testes de cada execução:**
  1. *Inicialização:* tempo de `import`, de criar o capturador e da primeira foto.
  2. *Velocidade máxima* ({num(settings['duration'], 0)} s): captura em laço, sem pausa. Mede a latência
     de cada chamada, chamadas por segundo e **quadros novos por segundo** (fotos diferentes da anterior,
     o que mostra se a biblioteca acompanha o jogo ou só repete a mesma imagem).
  3. *Cenário do agente* ({num(settings['duration'], 0)} s): captura a {num(agent_fps, 0)} FPS, como um
     agente faria, e mede o uso de CPU do processo (100% = um núcleo lógico inteiro).
  4. *Mostra o jogo?* Compara a foto com o conteúdo da janela do jogo naquele instante.
  5. *Jogo coberto:* abre uma janela magenta sempre no topo sobre o centro do jogo e verifica se ela
     aparece na foto.
- Antes de cada execução o jogo é trazido para a frente **sem receber o foco**, para que as capturas de
  tela vejam o jogo.

## Resultados

### Latência por captura

{image('latency', 'Latência por captura')}

{latency_table}

"Chamadas/s" é quantas vezes a função retornou por segundo; "Quadros novos/s" é quantas dessas fotos
eram diferentes da anterior. O jogo e o monitor estão a {refresh_hz} Hz, então nenhuma opção consegue mais
do que ~{refresh_hz} quadros novos por segundo: chamar mais rápido que isso só devolve a mesma imagem.

### Quadros novos por segundo

{image('new_frames', 'Quadros novos por segundo')}

### Uso de CPU no cenário do agente

{image('cpu', 'Uso de CPU a 30 FPS')}

{cpu_table}

"CPU por foto" é o tempo de processador gasto por captura no cenário do agente. "CPU no máximo" (laço
sem pausa) pode passar de 100% porque algumas bibliotecas capturam em threads próprias. O uso da placa
de vídeo e do processo do Windows que compõe a tela (DWM) não entra nessa conta. Os valores por execução
são a mediana das {settings['repeats']} repetições.
{recording_section(recording)}
### Inicialização

{image('startup', 'Tempo até a primeira foto')}

{startup_table}

### Jogo visível e jogo coberto

{background_table}{missed_note}

{image('covered', 'Fotos com o jogo coberto')}

As capturas de tela registram o quadrado magenta, porque fotografam o monitor. As capturas de janela
continuam mostrando o jogo por baixo dele.

### Fotos tiradas do jogo

{image('visible', 'Fotos do jogo tiradas por cada biblioteca')}
{failures_section}
## Código de cada opção

{code_section(summaries, agent_fps)}

## Observações e limitações

- **Janela minimizada:** nenhuma API consegue capturar um jogo minimizado (o Windows para de desenhar a
  janela). Deixe o jogo aberto, mesmo que atrás de outras janelas.
- **`windows-capture` e OpenCV:** chamar funções do OpenCV (ex.: `cv2.cvtColor`) dentro do
  `on_frame_arrived` derruba o processo com *access violation* na próxima sessão de captura (o OpenCV
  inicia o pool de threads dele na thread nativa da biblioteca). Copie o buffer com numpy dentro do
  callback e converta fora dele, como fazem `benchmark/capturers.py` e o `capturers.py` da raiz.
- **Threads do OpenCV:** com o número padrão de threads, as threads ociosas do OpenCV ficam girando
  depois de cada `cvtColor` e o tempo de CPU medido sobe e oscila muito. Num teste separado (3 execuções
  de cada, a {num(agent_fps, 0)} FPS), o `mss` foi de ~23% para ~58% de um núcleo e a
  `windows-capture (stream)` de ~24% para ~43%, sem ganho de latência que importe (1,4 ms contra 1,0 ms
  na `windows-capture`; igual nas outras).
- **`dxcam` e `bettercam`** imprimem um *access violation* do `comtypes` quando o Python fecha. É um
  erro de limpeza dessas bibliotecas na saída do processo e não afeta as capturas.
- **`windows-capture` no Windows 10** desenha uma borda amarela na janela enquanto captura (remover a
  borda exige Windows 11) e recusa `minimum_update_interval` ("not supported on this platform"), então
  a sessão sempre entrega todos os quadros do jogo, mesmo que o agente use menos. Isso explica parte
  do custo de CPU dela.
- **`d3dshot`** não foi incluída: está abandonada desde 2020 e não instala no Python {env['python']}.
- Os números valem para esta máquina e este estado do jogo; a ordem entre as opções tende a se manter,
  os valores absolutos não. A medição de CPU é só do processo Python.

## Como reproduzir

```powershell
.venv\\Scripts\\python.exe -m pip install -r benchmark\\requirements.txt
# Com o jogo aberto (pode estar atrás de outras janelas, mas não minimizado):
.venv\\Scripts\\python.exe -m benchmark.run
# Mais repetições, ou só algumas bibliotecas:
.venv\\Scripts\\python.exe -m benchmark.run --repeats 5 --duration 8
.venv\\Scripts\\python.exe -m benchmark.run --only dxcam windows-capture-stream printwindow
```

Os dados brutos (latência de cada captura) ficam em `benchmark/results/raw.json`, e
`python -m benchmark.report` refaz gráficos e relatório a partir deles.
"""


def build(raw_path: Path = RESULTS_DIR / "raw.json") -> Path:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    summaries, failures = summarize(raw)
    if not summaries:
        raise RuntimeError("Nenhuma execução bem-sucedida em " + str(raw_path))
    refresh_hz = raw["environment"]["refresh_hz"]
    agent_fps = raw["settings"]["agent_fps"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    charts: dict[str, str] = {}

    def chart_path(name: str, file_name: str) -> Path:
        charts[name] = f"results/{file_name}"
        return RESULTS_DIR / file_name

    latency_chart(summaries, chart_path("latency", "latencia.png"))
    bar_chart(
        summaries, lambda s: s.new_fps, higher_is_better=True,
        title="Quadros novos por segundo",
        subtitle=f"Fotos diferentes da anterior, capturando o mais rápido possível por "
                 f"{num(raw['settings']['duration'], 0)} s. Maior é melhor.",
        label=lambda s: num(s.new_fps), reference=(refresh_hz, f"{refresh_hz} Hz do monitor"),
        path=chart_path("new_frames", "quadros_novos.png"),
    )
    bar_chart(
        summaries, lambda s: s.agent_cpu,
        title=f"Uso de CPU com o agente a {num(agent_fps, 0)} FPS",
        subtitle="Percentual de um núcleo lógico usado pelo processo. Menor é melhor.",
        label=lambda s: f"{num(s.agent_cpu)}%" + ("" if s.agent_rate >= agent_fps * 0.95
                                                    else f"  (só {num(s.agent_rate)} FPS)"),
        path=chart_path("cpu", "cpu_agente.png"),
    )
    bar_chart(
        summaries, lambda s: s.startup_ms,
        title="Tempo até a primeira foto",
        subtitle="import + criação do capturador + primeira captura, em ms (mediana). Menor é melhor.",
        label=lambda s: f"{ms(s.startup_ms)} ms",
        path=chart_path("startup", "inicializacao.png"),
    )
    # The photos come from the last repetition of each library, so the captions use that run's result
    if not contact_sheet(summaries, "-coberto", "Fotos com uma janela magenta por cima do jogo",
                         lambda s: f"{s.label}  ·  "
                         + ("✓ vê o jogo" if s.sample_sees_covered_game else "✗ vê a janela da frente"),
                         chart_path("covered", "fotos_jogo_coberto.jpg")):
        charts.pop("covered")
    if not contact_sheet(summaries, "", "Uma foto do jogo por biblioteca",
                         lambda s: s.label + ("" if s.sample_shows_game else "  ·  ✗ outra janela estava na frente"),
                         chart_path("visible", "fotos_jogo.jpg")):
        charts.pop("visible")

    REPORT_PATH.write_text(write_markdown(raw, summaries, failures, charts, load_recording()), encoding="utf-8")
    return REPORT_PATH


if __name__ == "__main__":
    print(build())
