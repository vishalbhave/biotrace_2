"""
biotrace_parallel_engine.py
────────────────────────────────────────────────────────────────────────────────
Parallel chunk-processing engine for BioTrace v5.3+

Drop-in replacement for the sequential `for chunk in all_chunks` loop inside
extract_occurrences().  Key capabilities:

  • Hardware-aware concurrency — auto-detects available RAM + VRAM and scales
    max_workers accordingly so the host is never OOM-killed.
  • Adaptive throttling — backs off when free RAM falls below the safety floor.
  • Real-time ETA panel rendered into the Streamlit sidebar (or stdout if absent).
  • Thread-safe result collection with ordered reassembly.
  • Zero new hard dependencies — uses stdlib only (concurrent.futures, threading,
    time, psutil if available; VRAM via pynvml if available).

USAGE (replace the for-loop in extract_occurrences):
────────────────────────────────────────────────────
    from .parallel_engine import run_parallel_extraction

    results = run_parallel_extraction(
        all_chunks     = all_chunks,
        process_chunk  = process_chunk,          # the existing function
        chunk_kwargs   = dict(                   # shared keyword args
            schema_prompt    = schema_prompt,
            cite_str         = cite_str,
            provider         = provider,
            model_sel        = model_sel,
            api_key          = api_key,
            ollama_base_url  = ollama_base_url,
            use_thinker      = use_thinker,
            gna_pipeline     = gna_pipeline,
        ),
        log_cb         = log_cb,
        eta_panel      = True,                   # show ETA in Streamlit sidebar
    )
────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import time
import threading
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Any

# ── Optional hardware probes ──────────────────────────────────────────────────
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML = True
except Exception:
    _NVML = False


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware detection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HardwareProfile:
    total_ram_gb:   float = 4.0
    free_ram_gb:    float = 2.0
    total_vram_gb:  float = 0.0
    free_vram_gb:   float = 0.0
    cpu_cores:      int   = 2
    gpu_count:      int   = 0
    gpu_names:      list  = field(default_factory=list)

def probe_hardware() -> HardwareProfile:
    p = HardwareProfile()
    p.cpu_cores = os.cpu_count() or 2

    if _PSUTIL:
        vm = psutil.virtual_memory()
        p.total_ram_gb = vm.total / 1e9
        p.free_ram_gb  = vm.available / 1e9

    if _NVML:
        try:
            p.gpu_count = pynvml.nvmlDeviceGetCount()
            for i in range(p.gpu_count):
                h    = pynvml.nvmlDeviceGetHandleByIndex(i)
                info = pynvml.nvmlDeviceGetMemoryInfo(h)
                name = pynvml.nvmlDeviceGetName(h)
                if isinstance(name, bytes):
                    name = name.decode()
                p.gpu_names.append(name)
                p.total_vram_gb += info.total / 1e9
                p.free_vram_gb  += info.free  / 1e9
        except Exception:
            pass
    return p


def compute_max_workers(
    hw: HardwareProfile,
    provider: str = "ollama",
    ram_per_worker_gb: float = 0.35,
    vram_per_worker_gb: float = 0.5,
    floor_ram_gb: float = 1.0,          # always keep this much RAM free
) -> int:
    """
    Derive safe thread count from available resources.

    Logic:
      • RAM-bound:  (free_ram − floor) / ram_per_worker
      • VRAM-bound: free_vram / vram_per_worker  (only for local GPU providers)
      • CPU-bound:  cpu_cores × 2  (I/O-heavy LLM calls are not CPU-bound)
      • Hard floor: 1 worker; hard ceiling: 16 workers (avoids API rate limits)
    """
    usable_ram  = max(0, hw.free_ram_gb - floor_ram_gb)
    workers_ram = max(1, int(usable_ram / ram_per_worker_gb))

    if hw.free_vram_gb > 0 and provider in ("ollama", "local", "llama.cpp"):
        workers_vram = max(1, int(hw.free_vram_gb / vram_per_worker_gb))
        workers_hw   = min(workers_ram, workers_vram)
    else:
        workers_hw = workers_ram

    workers_cpu = hw.cpu_cores * 2
    n = min(workers_hw, workers_cpu, 16)
    return max(1, n)


# ─────────────────────────────────────────────────────────────────────────────
#  ETA tracker (thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

class ETATracker:
    """Running ETA estimate using exponential moving average of chunk durations."""

    def __init__(self, total: int):
        self.total      = total
        self.done       = 0
        self.skipped    = 0
        self.errors     = 0
        self.records    = 0
        self.start_time = time.monotonic()
        self._times:list[float] = []       # per-chunk durations
        self._lock      = threading.Lock()
        self._alpha     = 0.3              # EMA smoothing

    def tick(self, duration: float, n_records: int, status: str) -> None:
        with self._lock:
            self.done += 1
            if status == "skip":
                self.skipped += 1
            elif status == "error":
                self.errors += 1
            else:
                self.records += n_records
            self._times.append(duration)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def ema_duration(self) -> float:
        """Exponential moving average of recent chunk durations."""
        if not self._times:
            return 0.0
        ema = self._times[0]
        for t in self._times[1:]:
            ema = self._alpha * t + (1 - self._alpha) * ema
        return ema

    @property
    def eta_seconds(self) -> float:
        remaining = self.total - self.done
        if remaining <= 0 or not self._times:
            return 0.0
        return self.ema_duration * remaining

    def snapshot(self) -> dict:
        with self._lock:
            return dict(
                total    = self.total,
                done     = self.done,
                skipped  = self.skipped,
                errors   = self.errors,
                records  = self.records,
                elapsed  = self.elapsed,
                eta_s    = self.eta_seconds,
                pct      = self.done / max(self.total, 1),
            )


def _fmt_time(seconds: float) -> str:
    if seconds < 0 or math.isnan(seconds):
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ─────────────────────────────────────────────────────────────────────────────
#  Streamlit ETA panel (non-blocking, rendered in sidebar)
# ─────────────────────────────────────────────────────────────────────────────

def _make_streamlit_panel(tracker: ETATracker, hw: HardwareProfile, n_workers: int) -> None:
    """Render ETA panel once; caller loops and re-calls."""
    try:
        import streamlit as st
        snap = tracker.snapshot()
        pct  = snap["pct"]

        with st.sidebar:
            st.markdown("### ⚙️ Extraction Progress")
            st.progress(pct, text=f"{snap['done']} / {snap['total']} chunks")

            col1, col2, col3 = st.columns(3)
            col1.metric("⏱ Elapsed",   _fmt_time(snap["elapsed"]))
            col2.metric("⏳ ETA",       _fmt_time(snap["eta_s"]))
            col3.metric("📋 Records",   snap["records"])

            st.caption(
                f"Workers: **{n_workers}**  |  "
                f"Skipped: {snap['skipped']}  |  "
                f"Errors: {snap['errors']}  |  "
                f"RAM free: {hw.free_ram_gb:.1f} GB"
                + (f"  |  VRAM free: {hw.free_vram_gb:.1f} GB" if hw.free_vram_gb else "")
            )
    except Exception:
        pass   # Streamlit not available — stdout fallback below


def _stdout_panel(tracker: ETATracker, n_workers: int) -> None:
    snap = tracker.snapshot()
    bar_w = 30
    filled = int(bar_w * snap["pct"])
    bar = "█" * filled + "░" * (bar_w - filled)
    print(
        f"\r[BioTrace] [{bar}] {snap['done']:>3}/{snap['total']} "
        f"| {snap['records']} records "
        f"| elapsed {_fmt_time(snap['elapsed'])} "
        f"| ETA {_fmt_time(snap['eta_s'])} "
        f"| {n_workers}W",
        end="", flush=True
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Adaptive throttle — pause a worker if RAM is tight
# ─────────────────────────────────────────────────────────────────────────────

def _ram_ok(floor_gb: float = 1.0) -> bool:
    if not _PSUTIL:
        return True
    return psutil.virtual_memory().available / 1e9 >= floor_gb


# ─────────────────────────────────────────────────────────────────────────────
#  Worker wrapper
# ─────────────────────────────────────────────────────────────────────────────

def _worker(
    idx:         int,
    chunk:       Any,
    process_chunk: Callable,
    chunk_kwargs:  dict,
    floor_ram_gb:  float,
) -> tuple[int, Any, float]:
    """
    Runs one chunk through process_chunk().
    Backs off up to 10 s if RAM is low before starting.
    Returns (original_index, _ChunkResult, duration_seconds).
    """
    # Adaptive throttle — wait up to 10 s for RAM to recover
    for _ in range(20):
        if _ram_ok(floor_ram_gb):
            break
        time.sleep(0.5)

    text          = getattr(chunk, "context", None) or getattr(chunk, "text", str(chunk))
    section_label = getattr(chunk, "section", f"chunk-{getattr(chunk, 'chunk_id', idx) + 1}")
    candidate_locs= list(getattr(chunk, "candidate_localities", []))

    t0 = time.monotonic()
    result = process_chunk(
        text           = text,
        section_label  = section_label,
        candidate_locs = candidate_locs,
        **chunk_kwargs,
    )
    return idx, result, time.monotonic() - t0


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_parallel_extraction(
    all_chunks:    list,
    process_chunk: Callable,
    chunk_kwargs:  dict,
    log_cb:        Callable | None = None,
    eta_panel:     bool = True,
    provider:      str  = "ollama",
    floor_ram_gb:  float = 1.0,
    skip_no_species: bool = True,
) -> list[dict]:
    """
    Process chunks in parallel with hardware-adaptive worker count and
    real-time ETA display.

    Parameters
    ----------
    all_chunks      List of chunk objects (batches or flat_chunks from
                    ScientificPaperChunker / HierarchicalChunker / fallback).
    process_chunk   The existing process_chunk() function from main.
    chunk_kwargs    Dict of keyword args passed to every process_chunk() call
                    (schema_prompt, cite_str, provider, model_sel, …).
    log_cb          BioTrace logger callback.
    eta_panel       If True, render the ETA panel (Streamlit or stdout).
    provider        LLM provider name — used to tune VRAM worker estimate.
    floor_ram_gb    Always keep at least this much RAM free.
    skip_no_species Skip chunks where has_species=False (same logic as before).

    Returns
    -------
    Ordered list of occurrence dicts (same order as all_chunks input).
    """
    if log_cb is None:
        log_cb = lambda msg, lvl="ok": None

    # ── Hardware profile ──────────────────────────────────────────────────────
    hw         = probe_hardware()
    n_workers  = compute_max_workers(hw, provider=provider, floor_ram_gb=floor_ram_gb)

    log_cb(
        f"[ParallelEngine] {len(all_chunks)} chunks | "
        f"{n_workers} workers | "
        f"RAM {hw.free_ram_gb:.1f}/{hw.total_ram_gb:.1f} GB free"
        + (f" | VRAM {hw.free_vram_gb:.1f}/{hw.total_vram_gb:.1f} GB" if hw.gpu_count else "")
        + (f" | GPU: {', '.join(hw.gpu_names)}" if hw.gpu_names else "")
    )

    # ── Filter chunks (same skip logic as original loop) ─────────────────────
    active_indices = [
        i for i, chunk in enumerate(all_chunks)
        if not (
            skip_no_species
            and len(all_chunks) > 10
            and not getattr(chunk, "has_species", True)
        )
    ]
    n_skip_prefilter = len(all_chunks) - len(active_indices)
    if n_skip_prefilter:
        log_cb(f"[ParallelEngine] Pre-skipped {n_skip_prefilter} non-species chunks")

    tracker = ETATracker(total=len(active_indices))

    # ── Result store — pre-allocated to maintain ordering ────────────────────
    result_store: dict[int, list] = {}   # idx → records list
    error_ct = 0
    skip_ct  = n_skip_prefilter

    # ── ETA panel thread ─────────────────────────────────────────────────────
    _stop_panel = threading.Event()

    def _panel_loop():
        _streamlit_ok = False
        try:
            import streamlit as st
            _streamlit_ok = True
        except ImportError:
            pass

        while not _stop_panel.is_set():
            if _streamlit_ok and eta_panel:
                _make_streamlit_panel(tracker, hw, n_workers)
            elif eta_panel:
                _stdout_panel(tracker, n_workers)
            time.sleep(0.8)

    panel_thread = threading.Thread(target=_panel_loop, daemon=True)
    if eta_panel:
        panel_thread.start()

    # ── Parallel execution ────────────────────────────────────────────────────
    try:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(
                    _worker,
                    idx,
                    all_chunks[idx],
                    process_chunk,
                    chunk_kwargs,
                    floor_ram_gb,
                ): idx
                for idx in active_indices
            }

            for future in as_completed(futures):
                orig_idx = futures[future]
                try:
                    idx, result, duration = future.result()
                    n_rec = len(result.records) if result.status == "ok" else 0
                    tracker.tick(duration, n_rec, result.status)
                    result_store[idx] = result.records if result.status == "ok" else []

                    if result.status == "skip":
                        skip_ct  += 1
                    elif result.status == "error":
                        error_ct += 1
                        log_cb(f"[ParallelEngine] chunk {orig_idx} error: {result.error}", "warn")
                    else:
                        log_cb(
                            f"[ParallelEngine] chunk {orig_idx} "
                            f"→ {n_rec} records in {duration:.1f}s",
                            "ok",
                        )
                except Exception as exc:
                    error_ct += 1
                    result_store[orig_idx] = []
                    log_cb(f"[ParallelEngine] chunk {orig_idx} exception: {exc}", "warn")

    finally:
        _stop_panel.set()
        if eta_panel and not _stdout_panel.__module__:
            print()   # newline after stdout bar

    # ── Reassemble in original chunk order ───────────────────────────────────
    records: list[dict] = []
    for idx in sorted(result_store):
        records.extend(result_store[idx])

    snap = tracker.snapshot()
    log_cb(
        f"[ParallelEngine] Done — {snap['records']} records | "
        f"{snap['done']} chunks in {_fmt_time(snap['elapsed'])} | "
        f"errors: {error_ct} | skipped: {skip_ct}"
    )
    return records
