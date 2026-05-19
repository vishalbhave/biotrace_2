# BioTrace v5.3 Integration: Quick Reference

## Files Created
1. **biotrace_biocenteric_extractor.py** — Drop-in module (ready to use)
2. **biotrace_llm_orchestration_strategy.md** — Full architectural guide

## Why This Fixes Your Problems

| Problem | Your System | This Solution |
|---------|------------|---|
| **Proximity Fallacy** | Character distance assigns locality | Context window per species (±800 chars) |
| **Primary/Secondary** | Rule-based, error-prone | Explicit LLM classification + keyword validator |
| **Context Loss** | String splitting destroys relationships | Preserves habitat/depth/season as attributes |

## Integration Steps

### Step 1: Import (biotrace_v53_2.py, line 74-99)
Add to the enhancement imports block:
```python
# After line 98 (existing dedup patch import)
from biotrace_biocenteric_extractor import BioCentricExtractor, classify_source_type
```

### Step 2: Insert BioCentricExtractor class (before line 970)
Copy **lines 1-200 from biotrace_biocenteric_extractor.py** into biotrace_v53_2.py, right before the `@_dc` decorator (line 970).

This places it before `process_chunk()` definition where it can be imported and used.

### Step 3: Modify process_chunk() — Species extraction block (lines 1019-1047)

**REPLACE this block:**
```python
    # NEW: Priority 1 - GNA-First Pipeline
    if gna_pipeline:
        sp_windows = gna_pipeline.extract_gna_verified_species_windowed(text)
        if sp_windows:
            unique_sps = list({sw.canonical_name for sw in sp_windows})
            species_hint_str = "\n".join(f"  • {n}" for n in unique_sps[:30])
            log_cb(f"    [GNA-First] {len(unique_sps)} verified species identified")

    if use_hf_ner and _BIODIVIZ_AVAILABLE:
        # ... rest of block
```

**WITH:**
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
                # Source type classification (post-LLM validation)
                for rec in biocentric_records:
                    if not rec.get("sourceType"):
                        rec["sourceType"] = classify_source_type(rec, augmented_text)
                    if rec["sourceType"] == "secondary_literature":
                        rec.setdefault("validationStatus", "secondary_citation")
                
                log_cb(f"    [BioCenter] {len(biocentric_records)} records extracted")
                return _ChunkResult(records=biocentric_records)
            
            # Fallback: use species list as hint
            species_hint_str = "\n".join(f"  • {n}" for n in unique_sps[:30])
            log_cb(f"    [GNA-First] {len(unique_sps)} verified species (BioCenter fallback)")

    if use_hf_ner and _BIODIVIZ_AVAILABLE:
        # ... rest of block unchanged
```

### Step 4: Update extract_occurrences() signature (line 1154)
Add two parameters:
```python
def extract_occurrences(
    # ... existing parameters ...
    use_biocentered:    bool = True,  # NEW in v5.3
    use_gna_first:      bool = False, # Already exists
    # ... rest unchanged ...
) -> list[dict]:
```

### Step 5: Post-extraction validation (after line 1143)
Add source type classification before schema parsing:
```python
        # ── Source Type Classification (v5.3) ──────────────────
        if data and isinstance(data, list):
            for rec in data:
                if isinstance(rec, dict) and "sourceType" not in rec:
                    rec["sourceType"] = classify_source_type(rec, augmented_text)
```

## Geocoding Integration (NO CHANGES NEEDED)
BioCentricExtractor output is already compatible with `geocoding_cascade.GeocodingCascade`:

```python
from geocoding_cascade import GeocodingCascade

geo = GeocodingCascade(
    geonames_db=GEONAMES_DB,
    pincode_txt=PINCODE_TXT,
    use_nominatim=True,
)

# BioCentricExtractor returns records with:
# - scientificName ✓
# - verbatimLocality ✓
# - sourceType (NEW: usable for filtering)
# - decimalLatitude/Longitude (None → will be filled)

geocoded = geo.geocode_batch(biocentric_records)
```

## Key Differences from Your Proposed Approach

Your Idea → Better Implementation:
- "LLM Orchestration Layer between Docling + GNA" → BioCentricExtractor dispatches **per-species** (not document-wide)
- "Extract species + localities in one pass" → **Two-pass**: GNA identifies species → BioCentricExtractor extracts their localities only
- "Separate primary from secondary in post-processing" → **Built into LLM prompt + validator** (explicit classification)
- "Complex integration" → **Single stateless class** (no patches, no global state)

## Testing Your Integration

```python
# Quick smoke test (add to biotrace_v53_2.py if desired)
def test_biocentered_integration():
    text = """
    METHODS: We studied three sites: Poshitra (intertidal), Narara (subtidal).
    RESULTS: E. carneum was abundant at Poshitra, depth 2-5m. 
    According to Mammen (1963), E. capillare occurs in South India.
    """
    
    extractor = BioCentricExtractor(context_window=800)
    records = extractor.extract_per_species(
        text=text,
        species_list=["E. carneum", "E. capillare"],
        provider="ollama",
        model_sel="qwen2.5:7b",
        api_key="",
        ollama_base_url="http://localhost:11434",
    )
    
    # Assertions
    assert len(records) == 2, "Should extract 2 species"
    
    # Find primary vs secondary
    primary = [r for r in records if r.get("sourceType") == "primary_collection"]
    secondary = [r for r in records if r.get("sourceType") == "secondary_literature"]
    
    assert len(primary) >= 1, "E. carneum should be primary"
    assert len(secondary) >= 1, "E. capillare should be secondary"
    
    print(f"✓ Extracted {len(primary)} primary + {len(secondary)} secondary records")
```

## Rollout Plan

**Week 1**: Deploy BioCentricExtractor, keep old code active
- Set `use_biocentered=True` in UI
- A/B test: compare with old method
- Monitor error rates, adjust LLM prompt if needed

**Week 2**: Make default
- `use_biocentered=True` by default
- Keep fallback for non-GNA documents

**Week 3+**: Deprecate old path (v5.4)
- Remove legacy `enrich_occurrences()` code
- Archive old geocoding approach

## Contact/Support
If your LLM outputs are malformed:
1. Check `context_window` (default 800 chars — adjust if too small/large)
2. Verify prompt in `_build_species_prompt()` (tweak instructions if needed)
3. Test with a strong model first (qwen2.5:7b, llama3.3)
4. Monitor logs for `[BioCenter] JSON parse failed` warnings

