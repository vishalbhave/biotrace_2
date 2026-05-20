"""
biotrace_biocenteric_extractor.py  (v5.4 — schema-enriched)
────────────────────────────────────────────────────────────────────────────────
Species-centric locality extractor for BioTrace.

Changes vs v5.3
───────────────
SCHEMA  _build_species_prompt
  • Added: taxonRank, substrate, depth_m, behaviouralNote, associatedTaxa,
           recordStatus, verbatimCoordinates, fieldNotes, rawTextEvidence
  • recordStatus encodes first-india / first-state / first-region /
    rediscovery / known — extracted from nearby Remarks sentences.
  • sourceType vocabulary aligned with main _SCHEMA_PROMPT:
    "Primary" | "Secondary" | "Uncertain"

extract_localities_via_llm
  • @staticmethod — no hidden self injection (Bug 4 fix).
  • Returns richer dict: primaryLocality, secondaryLocality,
    verbatim_coordinates, substrate, depth_m.
  • Strips <reasoning>/<think> blocks before JSON parsing.

BioCentricExtractor
  • _extract_from_windows() merges richer locality dict into occurrence record.
  • global_localities hint fed into every window prompt.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Standalone locality extractor  (called as BioCentricExtractor.extract_localities_via_llm)
# ─────────────────────────────────────────────────────────────────────────────

class BioCentricExtractor:

    # ── Enriched occurrence schema ─────────────────────────────────────────
    RECORD_SCHEMA = """\
For the species "{species}" mentioned in the text below, extract EVERY distinct occurrence.
Return ONLY a valid JSON array starting with '[' — no prose, no markdown fences.

Each element must have EXACTLY these keys (null for anything not explicitly stated):

{{
    "verbatimLocality":      "Exact place name as written. ONE location per record. Never blank.",
    "primaryLocality":       "Broadest named administrative unit (district / bay / island / state).",
    "secondaryLocality":     "Micro-habitat or station (e.g. 'intertidal rocky shore', 'Poshitra reef').",
    "verbatimCoordinates":   "Lat/lon string exactly as written in text, or null.",
    "decimalLatitude":       null,
    "decimalLongitude":      null,
    "habitat":               "coral reef | rocky intertidal | mangrove | seagrass | estuarine | pelagic | sandy-muddy | Not Reported",
    "substrate":             "Physical substrate the organism was on/in: sponge | rock | algae | live coral | eunicid tubes | sediment | … or null.",
    "depth_m":               "Depth as written (e.g. '20-27') or null.",
    "taxonRank":             "species | genus | subspecies | variety",
    "sourceType":            "Primary | Secondary | Uncertain",
    "recordStatus":          "first_record_india | first_record_state | first_record_region | rediscovery | known",
    "associatedTaxa":        "Other species in same sentence (predator, prey, host, symbiont) or null.",
    "behaviouralNote":       "Any behaviour observed (feeding, spawning, fouling, symbiosis) or null.",
    "eventDate":             "Collection date or season if mentioned (ISO 8601 preferred) or null.",
    "recordedBy":            "Collector / observer name if explicitly stated or null.",
    "collectionMethod":      "hand-collection | trawl | dredge | net | trap | … or null.",
    "fieldNotes":            "Any other verbatim contextual note worth preserving or null.",
    "rawTextEvidence":       "EXACT sentence(s) proving this occurrence — copy verbatim, max 3 sentences.",
    "comments":              "Additional interpretive note or null."
}}

RULES:
  1. ONE locality per JSON object — split multi-site sentences into separate records.
  2. sourceType:
       Primary   = authors themselves collected / directly observed.
       Secondary = citing a prior publication (look for '(Author, Year)' patterns).
       Uncertain = genuinely ambiguous.
  3. recordStatus: scan nearby Remarks text for phrases such as
       "recorded for the first time from India"    → first_record_india
       "new record for Gujarat / the state"        → first_record_state
       "new record for the coast / region"         → first_record_region
       "rediscovered" / "refound"                  → rediscovery
       all others                                  → known
  4. Do NOT invent data — extract only what is explicitly in the text.
  5. If zero occurrences found for "{species}", return [].

{loc_hint}

TEXT CONTEXT:
{context_window}

OUTPUT (JSON array only):
"""

    def __init__(self, context_window: int = 900, log_cb=None):
        self.context_window = context_window
        self.log_cb = log_cb or (lambda m, l="ok": None)

    # ── Standalone locality extractor ─────────────────────────────────────
    @staticmethod
    def extract_localities_via_llm(
        text_window: str,
        species_name: str,
        llm_client,
        model_name: str,
    ) -> Dict[str, Optional[str]]:
        """
        Extract locality + contextual metadata for *species_name* from *text_window*.

        Returns dict with keys:
          primary_locality, secondary_locality, verbatim_coordinates,
          substrate, depth_m
        """
        system_prompt = (
            "You are an expert biodiversity researcher. "
            "Extract the geographic location and collection context of the target species "
            "from the provided text. Resolve station codes to named places if a station "
            "table is present. Respond ONLY with valid JSON — no markdown, no preamble."
        )

        user_prompt = f"""
Target Species: {species_name}
Text Snippet:
\"\"\"{text_window}\"\"\"

Return a JSON object with EXACTLY these keys (null for anything not mentioned):
{{
    "primary_locality":      "Main named place where {species_name} was found.",
    "secondary_locality":    "Sub-site, station name, reef, or micro-habitat.",
    "verbatim_coordinates":  "Lat/lon exactly as written (e.g. '22°14\u203228.9\u2033N 68°57\u203223.4\u2033E'), or null.",
    "substrate":             "Physical substrate (e.g. 'sponge', 'live hard coral'). null if not stated.",
    "depth_m":               "Depth in metres as written (e.g. '20-27'). null if not stated."
}}
"""
        try:
            response = llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.05,
            )
            raw = response.choices[0].message.content or ""

            # Strip reasoning blocks
            raw = re.sub(r"<think>.*?</think>",                "", raw, flags=re.DOTALL)
            raw = re.sub(r"<\|thinking\|>.*?<\|/thinking\|>", "", raw, flags=re.DOTALL)
            raw = re.sub(r"<reasoning>.*?</reasoning>",        "", raw, flags=re.DOTALL)
            raw = raw.strip()

            fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            s, e  = raw.find("{"), raw.rfind("}")
            candidate = fence.group(1).strip() if fence else (raw[s:e + 1] if s != -1 and e > s else "")

            if candidate:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return {
                        "primary_locality":     obj.get("primary_locality"),
                        "secondary_locality":   obj.get("secondary_locality"),
                        "verbatim_coordinates": obj.get("verbatim_coordinates"),
                        "substrate":            obj.get("substrate"),
                        "depth_m":              obj.get("depth_m"),
                    }
        except Exception as exc:
            logging.error(f"[LLM Extractor] Failed for {species_name}: {exc}")

        return {k: None for k in
                ("primary_locality", "secondary_locality",
                 "verbatim_coordinates", "substrate", "depth_m")}

    # ── Public entry point ────────────────────────────────────────────────
    def extract_per_species(
        self,
        text: str,
        species_list: List[str],
        provider: str,
        model_sel: str,
        api_key: str,
        ollama_base_url: str,
        global_localities: List[str] | None = None,
        max_species: int = 30,
    ) -> List[dict]:
        if not text or not species_list:
            return []

        g_locs = global_localities or []
        all_records: List[dict] = []

        # Deduplicate abbreviated vs full names
        normalized: List[str] = []
        for sp in species_list:
            if not any(sp in ex or ex in sp for ex in normalized):
                normalized.append(sp)

        for species in normalized[:max_species]:
            species = species.strip()
            if not species:
                continue
            windows = self._find_species_context_windows(text, species)
            if not windows:
                self.log_cb(f"    [BioCenter/{species}] no text windows found", "debug")
                continue
            records = self._extract_from_windows(
                species, windows, provider, model_sel, api_key, ollama_base_url,
                global_localities=g_locs,
            )
            if records:
                all_records.extend(records)
                self.log_cb(
                    f"    [BioCenter/{species}] {len(records)} records "
                    f"from {len(windows)} windows", "ok"
                )

        self.log_cb(
            f"    [BioCenter] {len(all_records)} total records "
            f"from {len(normalized)} species", "ok"
        )
        return all_records

    # ── Context window finder ─────────────────────────────────────────────
    def _find_species_context_windows(
        self, text: str, species: str
    ) -> List[Tuple[int, int, str]]:
        windows: List[Tuple[int, int, str]] = []
        pattern = re.escape(species).replace(r"\ ", r"\s+")
        for match in re.finditer(pattern, text, re.IGNORECASE):
            s, e = match.span()
            ws = max(0, s - self.context_window)
            we = min(len(text), e + self.context_window)
            windows.append((s, e, text[ws:we]))
        return windows

    # ── Per-window LLM extraction ─────────────────────────────────────────
    def _extract_from_windows(
        self,
        species: str,
        windows: List[Tuple[int, int, str]],
        provider: str,
        model_sel: str,
        api_key: str,
        ollama_base_url: str,
        global_localities: List[str],
    ) -> List[dict]:
        try:
            from main import call_llm, _robust_json_extract  # type: ignore
        except ImportError:
            self.log_cb("[BioCenter] Could not import call_llm — is biotrace_v53_2_1 on path?", "warn")
            return []

        records: List[dict] = []
        seen: set[str] = set()

        for _, _, window_text in windows:
            prompt = self._build_species_prompt(species, window_text, global_localities)
            try:
                raw  = call_llm(prompt, provider, model_sel, api_key, ollama_base_url)
                data = _robust_json_extract(raw)

                for rec in data:
                    if not isinstance(rec, dict):
                        continue
                    loc = (rec.get("verbatimLocality") or "").strip()
                    if not loc:
                        continue

                    loc_norm = re.sub(
                        r"\b(coast|area|region|island|the|reef)\b", "", loc.lower()
                    )
                    loc_norm = re.sub(r"[^a-z0-9]", "", loc_norm)[:16]
                    src  = rec.get("sourceType", "Uncertain")
                    key  = f"{loc_norm}::{src}"
                    if key in seen:
                        continue
                    seen.add(key)

                    # Stamp required fields
                    rec["scientificName"]   = species
                    rec.setdefault("decimalLatitude",  None)
                    rec.setdefault("decimalLongitude", None)
                    rec.setdefault("sourceType",       "Uncertain")
                    rec.setdefault("recordStatus",     "known")
                    rec.setdefault("taxonRank",        _infer_taxon_rank(species))
                    records.append(rec)

            except json.JSONDecodeError:
                pass
            except Exception as exc:
                self.log_cb(f"    [BioCenter/{species}] window error: {exc}", "warn")

        return records

    # ── Prompt builder ────────────────────────────────────────────────────
    def _build_species_prompt(
        self,
        species: str,
        context_window: str,
        loc_context: List[str] | None = None,
    ) -> str:
        loc_hint = ""
        if loc_context:
            loc_hint = (
                "KNOWN LOCALITIES FROM THIS PAPER (geocoding hints only — do not fabricate "
                "occurrences for these if {species} is not mentioned there):\n"
                + "\n".join(f"  • {l}" for l in loc_context[:20])
            )
        return self.RECORD_SCHEMA.format(
            species        = species,
            loc_hint       = loc_hint,
            context_window = context_window,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _infer_taxon_rank(name: str) -> str:
    tokens = name.strip().split()
    if len(tokens) == 1:
        return "genus"
    if any(q in name.lower() for q in ("subsp.", "var.", "ssp.")):
        return "subspecies"
    return "species"


def merge_locality_dict_into_record(record: dict, loc_dict: dict) -> dict:
    """Merge extract_localities_via_llm() output into an occurrence record.
    Only fills empty / null fields — never overwrites existing values."""
    mapping = {
        "primary_locality":     "primaryLocality",
        "secondary_locality":   "secondaryLocality",
        "verbatim_coordinates": "verbatimCoordinates",
        "substrate":            "substrate",
        "depth_m":              "depth_m",
    }
    for src_key, dst_key in mapping.items():
        val = loc_dict.get(src_key)
        if val and not record.get(dst_key):
            record[dst_key] = val
    return record
