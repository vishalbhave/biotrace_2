# BioTrace LLM Orchestration: Architecture & Implementation Guide

## Problem Analysis & Root Causes

### 1. **Proximity Fallacy** (Current System)
**Current approach**: `enrich_occurrences()` uses character distance to assign localities.
```
Species: E. carneum
Found 300 chars away: "Poshitra"
→ Assumes relationship exists based on proximity
```

**Root cause**: Single-pass broad LLM extraction treats text position as semantic relationship strength.

**Better approach**: **Species-centric locality extraction** (reverse the direction)
- For EACH verified species, search its textual context window
- Extract only localities mentioned within that species's narrative scope
- Automatically rejects distant unrelated places

---

### 2. **Primary vs. Secondary Confusion**
**Current problem**: Rule-based `segregate_locality_string()` cannot distinguish:
- "We collected E. carneum at Poshitra" (Primary: direct collection)
- "E. capillare was previously reported from South India (Mammen, 1963)" (Secondary: citation)

**Root cause**: LLM prompt doesn't ask for source type classification.

**Better approach**: **Extract source type explicitly in the LLM response**
```json
{
  "scientificName": "E. carneum",
  "verbatimLocality": "Poshitra",
  "occurrenceStatus": "present",  // primary indicator
  "sourceType": "primary_collection",  // NEW: explicit classification
  "citation": null
}
```

---

### 3. **Context Destruction**
**Current problem**: String splitting loses nested relationships.
```
"Site A (Narara, intertidal)" 
→ After split: ["Site A", "Narara", "intertidal"]
→ Context lost: which site? which habitat belongs to which location?
```

**Root cause**: Regex-based parsing assumes flat structure.

**Better approach**: **LLM-guided structured extraction with context preservation**
- Extract habitat, depth, season, observer as attributes of the locality
- Return nested context, not split strings
- Validate habitat against known marine ecosystems

---

## Proposed Architecture: Species-Centric Two-Pass Extraction

### Pass 1: Verified Species Inventory
```
Markdown → GNA Verifier → Canonical species list with confidence
```
**Benefit**: Know exactly which species to look for (eliminates false positives)

### Pass 2: Per-Species Locality Extraction (NEW LAYER)
```
For EACH verified species {
    Extract: [locality, habitat, collection_method, source_type, date, observer]
    Classify: [primary_collection | secondary_literature | type_specimen]
    Context: preserve surrounding text for validation
}
```

### Pass 3: Geocoding + Validation (EXISTING - unchanged)
```
verbatimLocality → [Pincode → GeoNames → Nominatim] → decimalLat/Lon
Validate: ocean bbox, state bbox, pincode match
```

---

## Implementation Strategy

### Architecture Overview
```
biotrace_v53_2.py (MAIN)
  ├─ extract_occurrences() [lines 1154-1280] — UNCHANGED orchestrator
  ├─ process_chunk() [lines 981-1149] — REFACTORED to dispatch to new layer
  │
  └─ NEW LAYER: BioCentricExtractor (lines INSERT-POINT-1)
      ├─ _build_species_context_windows()  — find text windows per species
      ├─ _extract_species_localities()     — LLM call (per-species prompt)
      ├─ _classify_source_type()           — primary vs secondary classifier
      └─ _preserve_nested_context()        — habitat/depth/season extraction

biotrace_locality_ner.py (EXISTING - minimal changes)
  ├─ LocalityNER._expand() [lines X-Y] — ADD post-extraction validation
  └─ (No core logic changes needed)

geocoding_cascade.py (EXISTING - unchanged)
  └─ GeocodingCascade.geocode_batch() — drop-in compatible
```

---

## Integration Points in biotrace_v53_2.py

### CHANGE 1: Insert BioCentricExtractor class (before `process_chunk` definition)
**Location**: Lines 970-981 (BEFORE the `_ChunkResult` dataclass)

```python
# ─────────────────────────────────────────────────────────────────────────────
#  NEW: SPECIES-CENTRIC LOCALITY EXTRACTION LAYER (v5.3)
# ─────────────────────────────────────────────────────────────────────────────

class BioCentricExtractor:
    """
    Per-species locality extraction to address:
      1. Proximity Fallacy: context window per species (not global position)
      2. Primary/Secondary: explicit source_type classification
      3. Context Destruction: preserve habitat/depth/season nesting
    """
    
    def __init__(self, context_window: int = 800, log_cb=None):
        """
        context_window: chars before/after species mention to consider as its scope.
                       Typical: 800 (covers one paragraph).
        """
        self.context_window = context_window
        self.log_cb = log_cb or (lambda m, l="ok": None)
    
    def extract_per_species(
        self,
        text: str,
        species_list: list[str],
        provider: str,
        model_sel: str,
        api_key: str,
        ollama_base_url: str,
    ) -> list[dict]:
        """
        For each species in species_list, extract its localities + source type.
        
        Returns: list of records with scientificName + nested context preserved.
        """
        all_records = []
        
        for species in species_list:
            # Step 1: Find text windows where species is mentioned
            windows = self._find_species_context_windows(text, species)
            if not windows:
                self.log_cb(f"    [BioCenter] {species}: no text windows found", "debug")
                continue
            
            # Step 2: Extract localities + source type from those windows
            species_records = self._extract_from_windows(
                species, windows, provider, model_sel, api_key, ollama_base_url
            )
            all_records.extend(species_records)
        
        self.log_cb(f"    [BioCenter] {len(all_records)} records from {len(species_list)} species")
        return all_records
    
    def _find_species_context_windows(self, text: str, species: str) -> list[tuple[int, int, str]]:
        """
        Find all occurrences of species name and return (start, end, window_text).
        window = text[max(0, start-context_window) : min(len(text), end+context_window)]
        """
        import re
        windows = []
        
        # Escape species name for regex (e.g., "E. carneum" → "E\. carneum")
        pattern = re.escape(species)
        
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start, end = match.span()
            window_start = max(0, start - self.context_window)
            window_end = min(len(text), end + self.context_window)
            window_text = text[window_start:window_end]
            windows.append((start, end, window_text))
        
        return windows
    
    def _extract_from_windows(
        self,
        species: str,
        windows: list[tuple[int, int, str]],
        provider: str,
        model_sel: str,
        api_key: str,
        ollama_base_url: str,
    ) -> list[dict]:
        """
        For each context window, send to LLM with species-specific prompt.
        Return deduplicated locality records.
        """
        records = []
        seen_localities = set()
        
        for idx, (_, _, window_text) in enumerate(windows):
            prompt = self._build_species_prompt(species, window_text)
            
            try:
                raw = call_llm(prompt, provider, model_sel, api_key, ollama_base_url)
                # Clean response
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                raw = re.sub(r"<\|thinking\|>.*?<\|/thinking\|>", "", raw, flags=re.DOTALL).strip()
                
                # Extract JSON array
                fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
                if fence:
                    raw = fence.group(1).strip()
                
                data = json.loads(raw) if raw.startswith("[") else []
                
                # Deduplicate by locality + source type
                for rec in data:
                    if isinstance(rec, dict):
                        loc = rec.get("verbatimLocality", "").strip()
                        src_type = rec.get("sourceType", "primary_collection")
                        key = (loc, src_type)
                        
                        if key not in seen_localities and loc:
                            rec["scientificName"] = species
                            records.append(rec)
                            seen_localities.add(key)
            
            except json.JSONDecodeError:
                self.log_cb(f"    [BioCenter] {species} window {idx}: JSON parse failed", "warn")
            except Exception as exc:
                self.log_cb(f"    [BioCenter] {species} window {idx}: {exc}", "warn")
        
        return records
    
    def _build_species_prompt(self, species: str, context_window: str) -> str:
        """
        Build a focused prompt for extracting localities of a single species.
        """
        return f"""You are extracting occurrence data for biological specimens.

TASK: For the species "{species}" mentioned in the text below, extract ALL of its:
  • Exact locations (locality names, site codes, coordinates)
  • Associated metadata (habitat, depth, season, collection method, observer)
  • Source type (primary collection vs. secondary literature citation)

RULES:
1. Output ONLY valid JSON array — no prose, no markdown, no explanations.
2. For each distinct locality associated with "{species}":
   {{
     "verbatimLocality": "Exact place name as written",
     "habitat": "Habitat type if mentioned (e.g., intertidal, 50m depth)",
     "collectionMethod": "How specimen was obtained (e.g., hand-collection, trawl)",
     "observer": "Collector/observer name if given",
     "eventDate": "Date or season if mentioned",
     "sourceType": "primary_collection OR secondary_literature OR type_specimen OR unknown",
     "decimalLatitude": null,
     "decimalLongitude": null,
     "comments": "Any additional context"
   }}
3. If sourceType is "secondary_literature", include the citation in comments.
4. If a location is mentioned multiple times with different metadata, output BOTH records.
5. Do NOT invent data — extract only what is explicitly stated.

TEXT CONTEXT:
{context_window}

OUTPUT (valid JSON array only):
"""


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY: Source Type Classifier (post-LLM validation)
# ─────────────────────────────────────────────────────────────────────────────

def classify_source_type(record: dict, text_context: str = "") -> str:
    """
    Secondary validation: if LLM sourceType is ambiguous, check keywords.
    
    Returns: "primary_collection" | "secondary_literature" | "type_specimen" | "unknown"
    """
    st = record.get("sourceType", "unknown").lower().strip()
    if st in ("primary_collection", "secondary_literature", "type_specimen"):
        return st
    
    # Fallback: keyword search in comments/metadata
    comments = (record.get("comments") or "") + " " + text_context
    comments_lower = comments.lower()
    
    if any(w in comments_lower for w in ["cited", "reported", "previously", "according to", "(author, year)", "literature"]):
        return "secondary_literature"
    elif any(w in comments_lower for w in ["collected", "collection", "we collected", "specimen", "deposit"]):
        return "primary_collection"
    elif any(w in comments_lower for w in ["holotype", "type specimen", "syntype"]):
        return "type_specimen"
    else:
        return "unknown"
```

---

### CHANGE 2: Refactor `process_chunk()` to dispatch to BioCentricExtractor
**Location**: Lines 1019-1047 (species_hint_str assembly block)

**REPLACE** this block:
```python
    # NEW: Priority 1 - GNA-First Pipeline
    if gna_pipeline:
        sp_windows = gna_pipeline.extract_gna_verified_species_windowed(text)
        if sp_windows:
            # Deduplicate canonical names for the LLM hint
            unique_sps = list({sw.canonical_name for sw in sp_windows})
            species_hint_str = "\n".join(f"  • {n}" for n in unique_sps[:30])
            log_cb(f"    [GNA-First] {len(unique_sps)} verified species identified")
```

**WITH**:
```python
    # NEW: Priority 1 - GNA-First + Species-Centric Extraction (v5.3)
    if gna_pipeline:
        sp_windows = gna_pipeline.extract_gna_verified_species_windowed(text)
        if sp_windows:
            unique_sps = list({sw.canonical_name for sw in sp_windows})
            
            # DISPATCH TO BIOCENTRICEXTRACTOR
            biocentered = BioCentricExtractor(context_window=800, log_cb=log_cb)
            biocentric_records = biocentered.extract_per_species(
                text=augmented_text,
                species_list=unique_sps[:30],
                provider=provider,
                model_sel=model_sel,
                api_key=api_key,
                ollama_base_url=ollama_base_url,
            )
            
            if biocentric_records:
                log_cb(f"    [GNA-First+BioCenter] {len(biocentric_records)} records extracted")
                # For now, we return early with these records
                # In production, merge with other extraction methods
                return _ChunkResult(records=biocentric_records)
            
            # Fallback: use species list as hint
            species_hint_str = "\n".join(f"  • {n}" for n in unique_sps[:30])
            log_cb(f"    [GNA-First] {len(unique_sps)} verified species identified (no BioCenter results)")
```

---

### CHANGE 3: Update `extract_occurrences()` signature
**Location**: Line 1154-1175 (function definition)

**ADD** two parameters:
```python
def extract_occurrences(
    # ... existing parameters ...
    use_gna_first:      bool = False,
    use_biocentered:    bool = True,  # NEW: enable species-centric extraction
    # ... remaining parameters ...
) -> list[dict]:
```

---

### CHANGE 4: Post-Extraction Validation Hook
**Location**: After line 1143 (after `log_cb(f"  [{section_label}] {len(data)} records")`)

**ADD**:
```python
        # ── Source Type Classification & Validation (v5.3) ──────────────────
        for rec in data:
            if isinstance(rec, dict):
                # Validate and upgrade source_type if needed
                rec["sourceType"] = classify_source_type(rec, augmented_text)
                
                # Flag secondary literature for separate handling
                if rec["sourceType"] == "secondary_literature":
                    rec["validationStatus"] = "secondary_citation"  # separate track
```

---

## Integration with geocoding_cascade.py (UNCHANGED)

The `BioCentricExtractor` output is already compatible:
```python
{
    "scientificName": "E. carneum",
    "verbatimLocality": "Poshitra, intertidal",  ← can be split by GeoNames/Pincode
    "decimalLatitude": None,                      ← filled by GeocodingCascade
    "decimalLongitude": None,                     ← filled by GeocodingCascade
    "habitat": "intertidal",                      ← preserved for context
    "sourceType": "primary_collection",           ← available for filtering
}
```

Pass to existing `geocoding_cascade.GeocodingCascade.geocode_batch()`:
```python
from geocoding_cascade import GeocodingCascade

geo_cascade = GeocodingCascade(
    geonames_db=GEONAMES_DB,
    pincode_txt=PINCODE_TXT,
    use_nominatim=True,
)

biocentric_records = biocentered.extract_per_species(...)
geocoded_records = geo_cascade.geocode_batch(biocentric_records)
```

---

## Why This Is Better Than Your Proposed Approach

| Dimension | Your Approach | This Approach |
|-----------|---------------|---------------|
| **Proximity Problem** | LLM tries to infer relationships from spatial context | Species context windows eliminate false positives |
| **Primary/Secondary** | Added to LLM prompt | Explicit structured output + post-validation classifier |
| **Integration** | New orchestration layer | Modular class, drops into existing pipeline |
| **Failure Mode** | If LLM fails on one species, entire chunk may fail | Per-species fallback — one bad species doesn't kill others |
| **Validation** | Single post-parse filter | Dual validation: LLM + keyword classifier |
| **Geocoding Compatibility** | Requires adapter | Direct — no schema changes needed |

---

## Testing Strategy

### Unit Test 1: Context Window Accuracy
```python
def test_context_window_extraction():
    text = "In 2020, E. carneum was found at Poshitra. E. capillare is unrelated."
    bio = BioCentricExtractor(context_window=100)
    windows = bio._find_species_context_windows(text, "E. carneum")
    assert "Poshitra" in windows[0][2]
    assert "capillare" not in windows[0][2]  # Different context
```

### Unit Test 2: Source Type Classification
```python
def test_source_type_classifier():
    rec1 = {"comments": "According to Smith (1990), E. carneum is found at X"}
    assert classify_source_type(rec1) == "secondary_literature"
    
    rec2 = {"comments": "We collected E. carneum at Poshitra on 2023-05-15"}
    assert classify_source_type(rec2) == "primary_collection"
```

### Integration Test: End-to-End
```python
markdown = """
METHODS
We surveyed three sites: Poshitra (intertidal), Narara (subtidal).

RESULTS
E. carneum was abundant at Poshitra, depth 2-5m.
E. capillare was rare. According to Mammen (1963), this species occurs in South India.
"""

occurrences = extract_occurrences(
    markdown,
    use_biocentered=True,
    use_gna_first=True,
    # ...
)

assert len(occurrences) == 2
assert occurrences[0]["scientificName"] == "E. carneum"
assert occurrences[0]["sourceType"] == "primary_collection"
assert occurrences[1]["sourceType"] == "secondary_literature"
```

---

## Phased Rollout

### Phase 1: Parallel Mode (this week)
- Deploy `BioCentricExtractor` alongside existing code
- Run both pipelines on same text
- Compare results (A/B test)
- Adjust LLM prompt based on real data

### Phase 2: Gradual Switching (next iteration)
- Make `use_biocentered` default to `True`
- Keep fallback to old method for non-GNA documents
- Monitor error rates

### Phase 3: Full Migration (v5.4)
- Remove old `enrich_occurrences()` path
- Consolidate validation logic
- Archive legacy code

