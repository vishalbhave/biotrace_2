"""
biotrace_v53_2_1_parallel_patch.py
────────────────────────────────────────────────────────────────────────────────
Minimal patch to apply to biotrace_v53_2_1.py:

  1. Import the parallel engine at module top.
  2. Replace the sequential for-loop in extract_occurrences() with
     run_parallel_extraction().
  3. Extend _SCHEMA_PROMPT with new BioTrace v5.4 schema fields.

HOW TO APPLY
────────────
Run:
    python biotrace_v53_2_1_parallel_patch.py

This script patches biotrace_v53_2_1.py **in place** and writes a backup to
biotrace_v53_2_1.py.pre_parallel_bak before making any changes.
"""

import re
import shutil
import sys
from pathlib import Path

TARGET = Path("biotrace_v53_2_1.py")


# ─────────────────────────────────────────────────────────────────────────────
#  Patch 1 — import parallel engine (after existing imports block)
# ─────────────────────────────────────────────────────────────────────────────

IMPORT_ANCHOR = "from biotrace_progress_logger import BioTraceLogger, render_species_progress_panel"

IMPORT_INJECTION = """\
from biotrace_progress_logger import BioTraceLogger, render_species_progress_panel

# ── Parallel extraction engine (v5.4) ────────────────────────────────────────
try:
    from biotrace_parallel_engine import run_parallel_extraction, probe_hardware
    _PARALLEL_ENGINE_AVAILABLE = True
except ImportError:
    _PARALLEL_ENGINE_AVAILABLE = False
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Patch 2 — replace sequential for-loop with parallel runner
#
#  The existing loop in extract_occurrences() looks like:
#
#    results:  list[dict] = []
#    error_ct = skip_ct   = 0
#
#    all_chunks = batches if batches else flat_chunks
#
#    for chunk in all_chunks:
#        text           = getattr(chunk, "context", None) or ...
#        ...
#        result = process_chunk(...)
#        if result.status == "skip":  ...
#        elif result.status == "error": ...
#        else: results.extend(result.records)
#
#    log_cb(f"[Extract] Raw total: ...")
#
# ─────────────────────────────────────────────────────────────────────────────

OLD_LOOP = '''\
    results:  list[dict] = []
    error_ct = skip_ct   = 0
 
 
    all_chunks = batches if batches else flat_chunks
 
    for chunk in all_chunks:
 
        text           = getattr(chunk, "context", None) or getattr(chunk, "text", str(chunk))
        section_label  = getattr(chunk, "section", f"chunk-{getattr(chunk, 'chunk_id', 0) + 1}")
        candidate_locs = list(getattr(chunk, "candidate_localities", []))
 
        # Skip non-species chunks in large documents (LLM call budget)
        if len(all_chunks) > 10 and not getattr(chunk, "has_species", True):
            skip_ct += 1
            continue
    
    
        result = process_chunk(
            text=text, 
            section_label=section_label,
            schema_prompt=schema_prompt, 
            cite_str=cite_str,
            provider=provider, 
            model_sel=model_sel,
            api_key=api_key, 
            ollama_base_url=ollama_base_url,
            use_thinker=use_thinker, 
            candidate_locs=candidate_locs, 
            log_cb=log_cb,
            gna_pipeline=gna_pipeline,  # <--- PASS IT HERE
        )
 
        if result.status == "skip":
            skip_ct += 1
        elif result.status == "error":
            error_ct += 1
        else:
            results.extend(result.records)
 
    log_cb(
        f"[Extract] Raw total: {len(results)} "
        f"| errors: {error_ct} | skipped: {skip_ct}"
    )'''

NEW_LOOP = '''\
    results:  list[dict] = []
    error_ct = skip_ct   = 0

    all_chunks = batches if batches else flat_chunks

    # ── v5.4: hardware-adaptive parallel extraction ───────────────────────────
    _use_parallel = (
        _PARALLEL_ENGINE_AVAILABLE
        and len(all_chunks) > 1
        and st.session_state.get("use_parallel_engine", True)
    )

    if _use_parallel:
        results = run_parallel_extraction(
            all_chunks    = all_chunks,
            process_chunk = process_chunk,
            chunk_kwargs  = dict(
                schema_prompt   = schema_prompt,
                cite_str        = cite_str,
                provider        = provider,
                model_sel       = model_sel,
                api_key         = api_key,
                ollama_base_url = ollama_base_url,
                use_thinker     = use_thinker,
                log_cb          = log_cb,
                gna_pipeline    = gna_pipeline,
            ),
            log_cb   = log_cb,
            provider = provider,
            eta_panel = True,
        )
        log_cb(
            f"[Extract] Raw total: {len(results)} (parallel engine)"
        )
    else:
        # ── Fallback: original sequential loop ───────────────────────────────
        for chunk in all_chunks:
            text           = getattr(chunk, "context", None) or getattr(chunk, "text", str(chunk))
            section_label  = getattr(chunk, "section", f"chunk-{getattr(chunk, 'chunk_id', 0) + 1}")
            candidate_locs = list(getattr(chunk, "candidate_localities", []))

            if len(all_chunks) > 10 and not getattr(chunk, "has_species", True):
                skip_ct += 1
                continue

            result = process_chunk(
                text=text,
                section_label=section_label,
                schema_prompt=schema_prompt,
                cite_str=cite_str,
                provider=provider,
                model_sel=model_sel,
                api_key=api_key,
                ollama_base_url=ollama_base_url,
                use_thinker=use_thinker,
                candidate_locs=candidate_locs,
                log_cb=log_cb,
                gna_pipeline=gna_pipeline,
            )

            if result.status == "skip":
                skip_ct += 1
            elif result.status == "error":
                error_ct += 1
            else:
                results.extend(result.records)

        log_cb(
            f"[Extract] Raw total: {len(results)} "
            f"| errors: {error_ct} | skipped: {skip_ct}"
        )'''


# ─────────────────────────────────────────────────────────────────────────────
#  Patch 3 — extend _SCHEMA_PROMPT with new v5.4 fields
#
#  We insert the new fields just before the MANDATORY COMPLETENESS CHECK line.
# ─────────────────────────────────────────────────────────────────────────────

OLD_SCHEMA_TAIL = '''\
  \"occurrenceType\"     — EXACTLY one of:
                           \"Primary\"   — Authors themselves collected/observed it in this study.
                           \"Secondary\" — Cited from a prior publication. Treat historical records in comparative statements as separate Secondary records.
                           \"Uncertain\" — Ambiguous; cannot determine if directly observed or cited.

MANDATORY COMPLETENESS CHECK before returning JSON:'''

NEW_SCHEMA_TAIL = '''\
  "occurrenceType"     — EXACTLY one of:
                           "Primary"   — Authors themselves collected/observed it in this study.
                           "Secondary" — Cited from a prior publication. Treat historical records in comparative statements as separate Secondary records.
                           "Uncertain" — Ambiguous; cannot determine if directly observed or cited.
  "primaryLocality"    — Broadest named administrative unit (district / bay / island / state).
  "secondaryLocality"  — Micro-habitat or station (e.g. "intertidal rocky shore", "Poshitra reef"). Null if same as verbatimLocality.
  "verbatimCoordinates"— Lat/lon string EXACTLY as written in text (e.g. "22°14′28.9″N 68°57′23.4″E"). Null if absent.
  "substrate"          — Physical substrate: sponge | rock | algae | live coral | eunicid tubes | sediment | … Null if unstated.
  "depth_m"            — Depth in metres as written (e.g. "20–27"). Null if unstated.
  "taxonRank"          — species | genus | subspecies | variety  (infer from name form).
  "recordStatus"       — EXACTLY one of:
                           "first_record_india"  — "first time from India", "new to Indian waters"
                           "first_record_state"  — "first time from Gujarat/Kerala/…"
                           "first_record_region" — "new to the coast/region"
                           "rediscovery"         — "rediscovered", "refound"
                           "known"               — all other cases
  "associatedTaxa"     — Other species in same sentence (predator, prey, host, symbiont). Null if none.
  "behaviouralNote"    — Observed behaviour (feeding, spawning, fouling, symbiosis). Null if none.
  "fieldNotes"         — Any other verbatim contextual note worth preserving. Null if none.
  "rawTextEvidence"    — EXACT sentence(s) proving this occurrence (copy verbatim, max 3 sentences).

MANDATORY COMPLETENESS CHECK before returning JSON:'''


# ─────────────────────────────────────────────────────────────────────────────
#  Patch 4 — add "Parallel Engine" toggle to Streamlit settings sidebar
#
#  Insert after the existing "use_gna_first" checkbox.
# ─────────────────────────────────────────────────────────────────────────────

OLD_SETTINGS_ANCHOR = 'use_gna_first    = st.session_state.get("use_gna_first", False)'

NEW_SETTINGS_BLOCK = '''\
                    use_gna_first    = st.session_state.get("use_gna_first", False),
                    # v5.4 parallel engine — toggle passed via session state set in sidebar
'''

# ─────────────────────────────────────────────────────────────────────────────
#  Sidebar UI snippet (insert in the Streamlit settings expander)
# ─────────────────────────────────────────────────────────────────────────────
SIDEBAR_ANCHOR = '# ── v5.9 HITL ML Framework Integration'

SIDEBAR_INJECTION = '''\
# ── v5.4 Parallel Engine toggle (sidebar) ────────────────────────────────────
# Place this block inside the settings expander in your Streamlit sidebar:
#
#   with st.sidebar.expander("⚙️ Extraction Settings", expanded=False):
#       st.session_state["use_parallel_engine"] = st.checkbox(
#           "⚡ Parallel extraction (faster)",
#           value=True,
#           help=(
#               "Uses all available CPU threads and GPU VRAM to process "
#               "document chunks in parallel. Disable if you see OOM errors."
#           ),
#       )
#       if _PARALLEL_ENGINE_AVAILABLE:
#           hw = probe_hardware()
#           st.caption(
#               f"Detected: {hw.cpu_cores} CPU cores | "
#               f"RAM {hw.free_ram_gb:.1f}/{hw.total_ram_gb:.1f} GB free"
#               + (f" | GPU VRAM {hw.free_vram_gb:.1f}/{hw.total_vram_gb:.1f} GB" if hw.gpu_count else "")
#           )
# ─────────────────────────────────────────────────────────────────────────────

# ── v5.9 HITL ML Framework Integration'''


# ─────────────────────────────────────────────────────────────────────────────
#  Apply all patches
# ─────────────────────────────────────────────────────────────────────────────

def apply():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found in current directory.", file=sys.stderr)
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(".py.pre_parallel_bak")
    shutil.copy(TARGET, backup)
    print(f"Backup written → {backup}")

    patches = [
        ("Import parallel engine",        IMPORT_ANCHOR,        IMPORT_INJECTION),
        ("Replace sequential loop",       OLD_LOOP,              NEW_LOOP),
        ("Extend _SCHEMA_PROMPT fields",  OLD_SCHEMA_TAIL,       NEW_SCHEMA_TAIL),
        ("Add sidebar UI comment",        SIDEBAR_ANCHOR,        SIDEBAR_INJECTION),
    ]

    for name, old, new in patches:
        if old not in src:
            print(f"  WARN [{name}] anchor not found — skipping (check for whitespace drift)")
        else:
            src = src.replace(old, new, 1)
            print(f"  OK   [{name}]")

    TARGET.write_text(src, encoding="utf-8")
    print(f"\nAll patches applied → {TARGET}")
    print("Run:  python -c \"import ast; ast.parse(open('biotrace_v53_2_1.py').read()); print('Syntax OK')\"")


if __name__ == "__main__":
    apply()
