# Scientific Wiki Engine Integration Guide

## Overview: Two-Tier Wiki System

Your BioTrace system will now have **two complementary wiki approaches** working together:

### Current System (BioTraceWikiUnified)
- **Purpose:** Fast, flexible wiki storage and updates
- **Citations:** Inline "Smith (2024) reports..."
- **Format:** SQLite database
- **Use case:** Internal tracking, rapid updates, multi-format imports

### NEW System (ScientificWikiEngine)
- **Purpose:** Publication-ready, research-paper-style documentation
- **Citations:** Numbered [1], [2], [3] with full bibliography
- **Format:** Markdown, HTML, BibTeX
- **Use case:** Conservation reports, publications, peer review, cited references

---

## Architecture: How They Work Together

```
Input Sources:
├─ Extracted occurrences (with source_reference_id)
├─ Normalized references (CitationNormalizer output)
└─ Document chunks (from HITL-approved extractions)
        ↓
    [ScientificWikiGenerator]
        ├─ _build_distribution_section() → each occ cited to source paper
        ├─ _build_taxonomy_section() → cites reference
        ├─ _build_ecology_section() → optional LLM synthesis with cites
        └─ _build_conservation_section()
        ↓
    [ScientificWikiArticle]
        ├─ Sections with [cite:ref_id] markers
        ├─ Citation index: {ref_id → [1], [2], [3]}
        └─ Reference dictionary: {ref_id → full bib object}
        ↓
    [Export]
        ├─ .to_markdown_with_bibliography() → Publication-ready MD
        ├─ .to_html_with_footnotes() → Clickable footnotes
        ├─ .to_bibtex() → For Zotero/Mendeley
        └─ [Optional] Push sections to BioTraceWikiUnified
```

---

## Integration Points: 5 Steps

### Step 1: Create Generator in Session State

**Add to `biotrace_v53.py` initialization:**

```python
# After existing wiki initialization
from biotrace_scientific_wiki_engine import ScientificWikiGenerator

# Session state
if 'scientific_wiki_gen' not in st.session_state:
    st.session_state.scientific_wiki_gen = ScientificWikiGenerator(
        wiki_root=WIKI_ROOT,
        call_llm=lambda p: call_llm(p, ...),  # Optional LLM for section synthesis
        log_cb=_lcb  # Log callback
    )
```

---

### Step 2: Generate on HITL Approval

**After HITL confirmation, instead of/alongside existing wiki update:**

```python
def on_hitl_approval(approved_occurrences, approved_metadata, document_text):
    """Called when HITL user approves species extraction."""
    
    # Your existing code...
    # wiki.update_from_occurrences(...)
    
    # NEW: Generate scientific wiki article
    from biotrace_scientific_wiki_engine import ScientificWikiGenerator
    from biotrace_citation_system import CitationNormalizer, OccurrenceLinker
    
    # Step 2.1: Get or deduplicate citations
    normalizer = CitationNormalizer()
    normalized_refs, _ = normalizer.deduplicate_batch(
        approved_metadata.get('citations', [])
    )
    
    # Step 2.2: Enrich occurrences with source references
    linker = OccurrenceLinker()
    enriched_occs = []
    for occ_dict in approved_occurrences:
        occ_record = enrich_occurrence_record(occ_dict, normalized_refs, linker)
        enriched_occs.append(occ_record)
    
    # Step 2.3: Generate scientific article
    species_name = approved_metadata.get('species_name', '')
    
    sci_wiki_gen = st.session_state.scientific_wiki_gen
    article = sci_wiki_gen.generate_species_article(
        species_name=species_name,
        occurrences=enriched_occs,
        references=normalized_refs,
        document_chunks=[document_text]  # For ecology section synthesis
    )
    
    return article  # Store in session state
```

---

### Step 3: Create Streamlit UI Tab

**Add to BioTrace main app (Tab: "Scientific Wiki"):**

```python
def render_scientific_wiki_tab():
    """Tab for viewing/exporting scientific wiki articles."""
    
    st.header("📖 Scientific Wiki Articles")
    st.write("""
    Generate research-paper-style wiki articles with full bibliography
    and traceable citations for each claim.
    """)
    
    # Get recent articles from session
    recent_articles = st.session_state.get('recent_sci_wiki_articles', {})
    
    if not recent_articles:
        st.info("No articles generated yet. Complete HITL approval to create articles.")
        return
    
    # Select article to view
    species_list = list(recent_articles.keys())
    selected_species = st.selectbox("Select species:", species_list)
    
    article = recent_articles[selected_species]
    
    # Display options
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if st.button("📄 View Markdown"):
            st.session_state.sci_wiki_view = 'markdown'
    
    with col2:
        if st.button("🌐 View HTML"):
            st.session_state.sci_wiki_view = 'html'
    
    with col3:
        if st.button("📚 View Bibliography"):
            st.session_state.sci_wiki_view = 'bibliography'
    
    with col4:
        if st.button("📋 View Statistics"):
            st.session_state.sci_wiki_view = 'stats'
    
    view_type = st.session_state.get('sci_wiki_view', 'preview')
    
    if view_type == 'markdown':
        md_text = article.to_markdown_with_bibliography()
        st.markdown(md_text)
        st.download_button(
            "⬇️ Download Markdown",
            md_text,
            file_name=f"{article.canonical_identifier}.md",
            mime="text/markdown"
        )
    
    elif view_type == 'html':
        html_text = article.to_html_with_footnotes()
        st.components.v1.html(html_text, height=800)
        st.download_button(
            "⬇️ Download HTML",
            html_text,
            file_name=f"{article.canonical_identifier}.html",
            mime="text/html"
        )
    
    elif view_type == 'bibliography':
        st.subheader("Full Bibliography")
        bibtex = article.to_bibtex()
        st.code(bibtex, language="bibtex")
        st.download_button(
            "⬇️ Download BibTeX",
            bibtex,
            file_name=f"{article.canonical_identifier}.bib",
            mime="text/plain"
        )
    
    elif view_type == 'stats':
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Total Citations", len(article._citation_index))
        
        with col2:
            sections = [s for s in [article.lead, article.taxonomy, 
                                   article.distribution, article.ecology, 
                                   article.conservation] if s]
            st.metric("Sections", len(sections))
        
        with col3:
            st.metric("Source Documents", len(article.source_documents))
        
        st.subheader("Citations by Section")
        
        for section in [article.taxonomy, article.distribution, 
                       article.ecology, article.conservation]:
            if section and section.cited_references:
                st.write(f"**{section.heading}:** {len(section.cited_references)} citations")
```

---

### Step 4: Export to File System

**Save articles for archival and distribution:**

```python
def save_scientific_wiki_article(article: ScientificWikiArticle, 
                                 export_dir: str = "./wiki_exports"):
    """Save scientific article in multiple formats."""
    
    from pathlib import Path
    import os
    
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    
    # Markdown version
    md_path = export_path / f"{article.canonical_identifier}.md"
    with open(md_path, 'w') as f:
        f.write(article.to_markdown_with_bibliography())
    
    # HTML version
    html_path = export_path / f"{article.canonical_identifier}.html"
    with open(html_path, 'w') as f:
        f.write(article.to_html_with_footnotes())
    
    # BibTeX version
    bib_path = export_path / f"{article.canonical_identifier}.bib"
    with open(bib_path, 'w') as f:
        f.write(article.to_bibtex())
    
    # Metadata
    metadata = {
        'title': article.title,
        'article_type': article.article_type,
        'generated': article.last_updated.isoformat(),
        'citations_count': len(article._citation_index),
        'files': {
            'markdown': str(md_path.name),
            'html': str(html_path.name),
            'bibtex': str(bib_path.name)
        }
    }
    
    meta_path = export_path / f"{article.canonical_identifier}_meta.json"
    import json
    with open(meta_path, 'w') as f:
        json.dump(metadata, f, indent=2)
    
    return {
        'markdown': str(md_path),
        'html': str(html_path),
        'bibtex': str(bib_path),
        'metadata': str(meta_path)
    }
```

---

### Step 5: Optional - Sync Back to BioTraceWikiUnified

**If you want sections in both systems:**

```python
def sync_scientific_to_biotrace_wiki(article: ScientificWikiArticle,
                                    bio_wiki):
    """
    Optionally populate BioTraceWikiUnified sections from scientific article.
    Maintains backward compatibility.
    """
    
    # Extract plain text (without [cite:XXX] markers)
    def clean_content(content):
        import re
        return re.sub(r'\[cite:[^\]]+\]', '', content)
    
    # Map sections
    updates = {}
    
    if article.taxonomy:
        updates['taxonomy'] = clean_content(article.taxonomy.content)
    
    if article.distribution:
        updates['distribution_habitat'] = clean_content(article.distribution.content)
    
    if article.ecology:
        updates['ecology'] = clean_content(article.ecology.content)
    
    # Metadata about citations
    updates['_citation_meta'] = {
        'scientific_citations_enabled': True,
        'total_citations': len(article._citation_index),
        'bibliography_available': True
    }
    
    # Update BioTraceWiki (use existing API)
    bio_wiki.update_locality_coords(...)  # or relevant method
    
    return updates
```

---

## Workflow: Complete Example

### Scenario: User extracts Marionia pambanensis from Nanda et al. 2023

```python
# 1. EXTRACTION (existing BioTrace)
# User uploads PDF → BioTrace extracts:
# - 2 occurrence records (Pamban 1932, Okha 2023)
# - 2 citations (Nanda et al. 2023, appears twice)

# 2. HITL REVIEW (existing)
# User reviews, approves in HITL tab

# 3. ON APPROVAL (NEW workflow)
approved_occs = [
    {
        'species_name': 'Marionia pambanensis',
        'locality': 'Pamban, Tamil Nadu',
        'country': 'India',
        'latitude': 8.5,
        'longitude': 78.0,
        'depth_m': 3.5,
        'event_date': '1932',
        'occurrence_type': 'Primary',
        'evidence_text': 'Type locality for the species',
        'source_citation': 'Nanda et al. 2023'
    },
    # ... second occurrence
]

# 4. CITATION DEDUPLICATION (NEW)
normalizer = CitationNormalizer()
normalized_refs, dedup_report = normalizer.deduplicate_batch([
    {'authors': 'S. Nanda; P. Hatkar; K. Vachhrajani', 'year': '2023', ...},
    {'authors': 'Nanda, S. and Hatkar, P.', 'year': '2023', ...}  # dup
])
# Result: 1 canonical reference + merge map

# 5. OCCURRENCE LINKING (NEW)
linker = OccurrenceLinker()
enriched_occs = []
for occ in approved_occs:
    occ_record = OccurrenceRecord(
        species_name=occ['species_name'],
        locality=occ['locality'],
        ...
        source_reference_id=normalized_refs[0].canonical_key,  # Link to source
        evidence_text=occ['evidence_text'],
        confidence=0.95
    )
    occ_record = linker.attempt_external_link(occ_record)  # GBIF/OBIS
    enriched_occs.append(occ_record)

# 6. SCIENTIFIC WIKI GENERATION (NEW)
sci_gen = ScientificWikiGenerator(wiki_root="./wiki")
article = sci_gen.generate_species_article(
    species_name='Marionia pambanensis',
    occurrences=enriched_occs,
    references=normalized_refs
)

# 7. EXPORT (NEW)
markdown = article.to_markdown_with_bibliography()
html = article.to_html_with_footnotes()

# OUTPUT:
# Markdown article with:
# - Distribution section citing each paper [1]
# - Full bibliography:
#   [1] Nanda, S., Hatkar, P., & Vachhrajani, K. (2023). "First distribution..."
#       Journal of the Marine Biological Association of India, 65(2), 2427-2439.
#
# HTML article with:
# - Clickable footnotes [1] → See reference section
# - Backreferences: [↑1] back to first mention
```

---

## File Organization

### BioTrace Directory Structure

```
biotrace/
├── biotrace_v53_2.py                    # Main app (existing)
├── biotrace_citation_system.py          # Citation dedup + linking
├── biotrace_scientific_wiki_engine.py   # NEW: Scientific wiki generation
│
├── biodiversity_data/
│   ├── biotrace_citations.db            # Normalized references
│   ├── biotrace_occurrence_links.db     # Occurrence-database links
│   ├── wiki/                            # Existing BioTraceWiki
│   │   ├── species/
│   │   └── ...
│   └── wiki_exports/                    # NEW: Scientific wiki exports
│       ├── marionia_pambanensis.md
│       ├── marionia_pambanensis.html
│       ├── marionia_pambanensis.bib
│       └── marionia_pambanensis_meta.json
```

---

## Data Flow for Publication

For publishing an article (e.g., conservation report):

```
Scientific Wiki Article
    ↓
.to_markdown_with_bibliography()
    ↓
(Pandoc/Markdown → PDF)
    ↓
[Publication-ready PDF with numbered citations]

Also available:
- .to_html_with_footnotes() → For web publishing
- .to_bibtex() → For citation manager import
```

---

## Testing & Validation

### Test Case 1: Citation Deduplication
```python
# Input: 2 raw citations (duplicate with formatting diff)
# Expected: 1 canonical reference + correct merge map
assert len(normalized_refs) == 1
assert dedup_report.duplicates_merged == 1
```

### Test Case 2: Article Generation
```python
# Input: Species name + 2 occurrences + 1 reference
# Expected: Article with citations, correct numbering
article = gen.generate_species_article(...)
assert len(article._citation_index) == 1
assert "[cite:" in article.distribution.content
assert article.to_markdown_with_bibliography().count("[1]") >= 2
```

### Test Case 3: Export Formats
```python
# Verify all formats generate valid output
md = article.to_markdown_with_bibliography()
html = article.to_html_with_footnotes()
bib = article.to_bibtex()

assert "## References" in md
assert "<sup>" in html and "<ol>" in html
assert "@article{" in bib
```

---

## Configuration

Add to config.yaml:

```yaml
scientific_wiki:
  enabled: true
  export_dir: "biodiversity_data/wiki_exports"
  formats: ["markdown", "html", "bibtex"]
  
  # Citation numbering style
  citation_style: "numbered"  # or "author-date"
  bibliography_format: "chicago"  # or "apa", "mla"
  
  # Content generation
  llm_synthesis_enabled: true
  auto_export_on_hitl_approval: true
  
  # Appearance
  include_toc: true  # Table of contents
  include_metadata: true  # Created date, authors, etc
  clickable_footnotes: true  # For HTML
```

---

## Support for All Biota

The scientific wiki system is **completely taxon-agnostic**:

**Marine:**
- Nudibranchs, corals, fish, cephalopods
- Link to OBIS for validation

**Terrestrial:**
- Mammals, reptiles, arthropods
- Link to GBIF, iDigBio

**Plants:**
- Angiosperms, ferns, mosses
- Link to POWO, BGCI

**Key: Same citation system for all** — just extend OccurrenceLinker with additional database APIs.

---

## Next Steps

1. **Week 1:** Copy files, integrate into your BioTrace installation
2. **Week 2:** Test on sample species (Marionia, etc.)
3. **Week 3:** Add UI tabs, test exports
4. **Week 4:** Generate baseline articles for priority species
5. **Week 5+:** Refine based on conservation team feedback

---

## Benefits Summary

✅ **Publication-Ready Content** — Wiki articles suitable for conservation reports  
✅ **Full Traceability** — Every statement linked to source paper  
✅ **Multiple Exports** — Markdown, HTML, BibTeX, PDF-ready  
✅ **Automated Citations** — No manual bibliography management  
✅ **Backward Compatible** — Works alongside existing BioTraceWiki  
✅ **Universal** — Works for all biota and regions  

---

## Questions?

Refer to:
- `biotrace_scientific_wiki_engine.py` for implementation details
- `biotrace_citation_system.py` for citation/linking system
- `biotrace_integration_guide.md` for general integration
