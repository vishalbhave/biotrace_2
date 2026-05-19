"""
biotrace_unified_wiki.py  —  BioTrace v6.0
════════════════════════════════════════════════════════════════════════════════
MERGED: biotrace_wiki_unified.py  +  biotrace_scientific_wiki_engine.py
────────────────────────────────────────────────────────────────────────────────

What changed vs v5.x:
  • Single class  BioTraceWikiUnified  retains ALL v5.5 capabilities
    (LLM enhancement, TAR, versioning, Streamlit rendering, folium maps)
  • ScientificWikiCapability  mixin grafted in — every article can now emit:
      - Numbered citations  [1], [2], [3]  (research-paper style)
      - Full Chicago-style bibliography
      - BibTeX export
      - HTML with clickable ↑ back-references
  • Bibliography is stored in the article JSON → survives wiki versioning
  • LocalityIdentifier  (new) — uses locality_hierarchy.db to:
      - Resolve raw verbatim locality strings → admin hierarchy
        (state / district / block / subdistrict)
      - Build an enriched locality string used by GeocodingCascade
      - Populate locality wiki articles with admin context
  • GeocodingCascade  (enhanced):
      - Tool 2 now uses the pre-built SQLite village index from
        locality_hierarchy.db (no lazy geopandas load required unless
        geometry centroid must be fetched from GPKG)
      - Admin-context disambiguation: uses district/state from
        admin_hierarchy table instead of the raw locality string alone

Architecture Overview:
  BioTraceWikiUnified
  ├─ LocalityIdentifier       (new — hierarchy DB lookups)
  ├─ TemporalAugmentedRetrieval  (TAR — from v5.5)
  ├─ ScientificWikiCapability (new — citation numbering + bibliography)
  ├─ LLMWikiArchitect         (from v5.5 — section-level enhancement)
  └─ render_unified_page()    (HTML + CSS — from v5.5)

  GeocodingCascade (v6.0)
  Tool 1: DMS parse
  Tool 2: hierarchy_db village fuzzy + admin disambiguation  ← enhanced
  Tool 3: IndianPincodeGeocoder
  Tool 4: GeoNames IN SQLite
  Tool 5: Nominatim fallback
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

logger = logging.getLogger("biotrace.wiki_v6")

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    import folium
    from folium.plugins import MarkerCluster, MiniMap
    _FOLIUM = True
except ImportError:
    _FOLIUM = False

try:
    import streamlit as st
    _ST = True
except ImportError:
    _ST = False

try:
    from rapidfuzz import process, fuzz
    _RAPIDFUZZ = True
except ImportError:
    _RAPIDFUZZ = False


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — SCIENTIFIC CITATION MODEL  (from scientific_wiki_engine.py)
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class WikiSectionScientific:
    """A wiki section with citation tracking."""
    heading: str
    content: str                       # markdown with [cite:ref_id] markers
    cited_references: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    confidence: float = 0.9
    generated_by: str = "agent"


class ScientificWikiCapability:
    """
    Mixin that adds research-paper-style bibliography to BioTraceWikiUnified.

    Usage (internally):
        article_json = ...            # dict stored in wiki_articles
        cap = ScientificWikiCapability()
        cap.register_reference(article_json, ref_dict)
        cap.render_bibliography_markdown(article_json)
    """

    # ── Reference registration ────────────────────────────────────────────

    @staticmethod
    def _ensure_bib(art: dict):
        """Ensure bibliography fields exist in article dict."""
        art.setdefault("bibliography", {})        # canonical_key → ref_dict
        art.setdefault("citation_index", {})      # canonical_key → int  (1, 2, 3…)

    @staticmethod
    def register_reference(art: dict, ref_dict: dict):
        """
        Register a reference. Automatically assigns next citation number.
        ref_dict must have 'canonical_key' + standard fields (authors, year,
        title, journal_name, doi, source_type, publisher …).
        """
        ScientificWikiCapability._ensure_bib(art)
        key = ref_dict.get("canonical_key", "")
        if not key:
            return
        art["bibliography"][key] = ref_dict
        if key not in art["citation_index"]:
            art["citation_index"][key] = len(art["citation_index"]) + 1

    @staticmethod
    def rebuild_citation_index(art: dict):
        """
        Scan all section text for [cite:KEY] markers and rebuild the
        numeric index in insertion order.
        """
        ScientificWikiCapability._ensure_bib(art)
        seen: Dict[str, int] = {}
        counter = 1
        secs = art.get("sections", {})
        for text in secs.values():
            for key in re.findall(r"\[cite:([^\]]+)\]", text):
                if key not in seen:
                    seen[key] = counter
                    counter += 1
        art["citation_index"] = seen

    # ── Text rendering ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_citations(text: str, index: dict) -> str:
        """Replace [cite:KEY] with [N] using the provided index."""
        for key, num in index.items():
            text = text.replace(f"[cite:{key}]", f"[{num}]")
        return text

    @staticmethod
    def _format_reference(ref: dict) -> str:
        """Format one reference as Chicago-style string."""
        authors = ref.get("authors", [])
        year    = ref.get("year", "n.d.")
        title   = ref.get("title", "Untitled")

        if not authors:
            author_str = "Anonymous"
        elif len(authors) == 1:
            author_str = authors[0]
        elif len(authors) <= 3:
            author_str = " & ".join(authors)
        else:
            author_str = f"{authors[0]} et al."

        st_   = ref.get("source_type", "journal")
        if st_ == "journal":
            j     = ref.get("journal_name", "")
            vol   = ref.get("volume", "")
            issue = ref.get("issue", "")
            pages = ref.get("pages", "")
            doi   = ref.get("doi", "")
            s = f'{author_str} ({year}). "{title}." *{j}*'
            if vol:
                s += f", {vol}"
                if issue: s += f"({issue})"
            if pages: s += f": {pages}"
            if doi:   s += f". https://doi.org/{doi}"
            return s + "."
        elif st_ == "book":
            pub = ref.get("publisher", "")
            return f"{author_str} ({year}). *{title}*. {pub}."
        else:
            return f"{author_str} ({year}). {title}."

    # ── Markdown export ───────────────────────────────────────────────────

    @staticmethod
    def render_bibliography_markdown(art: dict) -> str:
        """Return the full References section as markdown."""
        ScientificWikiCapability._ensure_bib(art)
        bib   = art.get("bibliography", {})
        index = art.get("citation_index", {})
        if not bib:
            return "\n## References\n\n*(No references registered)*\n"
        sorted_refs = sorted(bib.items(), key=lambda x: index.get(x[0], 9999))
        lines = ["\n## References\n"]
        for key, ref in sorted_refs:
            num = index.get(key)
            if num:
                lines.append(f"[{num}] {ScientificWikiCapability._format_reference(ref)}\n")
        return "\n".join(lines)

    @staticmethod
    def render_bibliography_html(art: dict) -> str:
        """Return HTML bibliography with anchor tags for back-links."""
        ScientificWikiCapability._ensure_bib(art)
        bib   = art.get("bibliography", {})
        index = art.get("citation_index", {})
        if not bib:
            return ""
        sorted_refs = sorted(bib.items(), key=lambda x: index.get(x[0], 9999))
        items = ""
        for key, ref in sorted_refs:
            num = index.get(key)
            if num:
                txt = ScientificWikiCapability._format_reference(ref)
                items += f"<li id='ref-{num}' class='footnote'>{txt}</li>\n"
        return f"<div class='bibliography'><h2>References</h2><ol>{items}</ol></div>"

    @staticmethod
    def render_section_html_with_citations(text: str, index: dict) -> str:
        """Replace [cite:KEY] with clickable superscript footnotes in HTML."""
        for key, num in index.items():
            marker = f"[cite:{key}]"
            link   = (f"<sup><a href='#ref-{num}' title='Reference [{num}]'>"
                      f"[{num}]</a></sup>")
            text = text.replace(marker, link)
        return text

    @staticmethod
    def to_bibtex(art: dict) -> str:
        """Export all registered references as BibTeX."""
        bib = art.get("bibliography", {})
        out = []
        for key, ref in bib.items():
            st_ = ref.get("source_type", "journal")
            authors = " and ".join(ref.get("authors", []))
            if st_ == "journal":
                out.append(
                    f"@article{{{key},\n"
                    f"  author  = {{{authors}}},\n"
                    f"  year    = {{{ref.get('year','')}}},\n"
                    f"  title   = {{{ref.get('title','')}}},\n"
                    f"  journal = {{{ref.get('journal_name','')}}},\n"
                    f"  volume  = {{{ref.get('volume','')}}},\n"
                    f"  number  = {{{ref.get('issue','')}}},\n"
                    f"  pages   = {{{ref.get('pages','')}}},\n"
                    f"  doi     = {{{ref.get('doi','')}}}\n"
                    f"}}"
                )
            else:
                out.append(
                    f"@book{{{key},\n"
                    f"  author    = {{{authors}}},\n"
                    f"  year      = {{{ref.get('year','')}}},\n"
                    f"  title     = {{{ref.get('title','')}}},\n"
                    f"  publisher = {{{ref.get('publisher','')}}}\n"
                    f"}}"
                )
        return "\n\n".join(out)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — LOCALITY IDENTIFIER  (new in v6.0)
# ═════════════════════════════════════════════════════════════════════════════

class LocalityIdentifier:
    """
    Uses  locality_hierarchy.db  (built by build_hierarchy_db.py) to:

      1. resolve_to_admin(verbatim_locality)
            → dict {state, district, block, subdistrict} or {}

      2. build_enriched_locality(verbatim_locality, occ_dict)
            → "Narara, Jamnagar District, Gujarat"
            This enriched string feeds GeocodingCascade Tool 2, improving
            fuzzy-match precision and disambiguation.

      3. populate_locality_article(wiki, verbatim_locality, sp_name, occ)
            → updates the wiki locality article with admin hierarchy info.

    The class is intentionally lightweight — it queries only the two
    lightweight tables (admin_hierarchy, villages) and never loads geodata.
    """

    def __init__(self, hierarchy_db: str = "biodiversity_data/locality_hierarchy.db"):
        self.db = hierarchy_db
        self._village_cache: Optional[Dict[str, dict]] = None  # name → row
        self._admin_cache:   Optional[List[dict]]      = None

    # ── Internal DB helpers ───────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not os.path.exists(self.db):
            raise FileNotFoundError(f"LocalityIdentifier: DB not found at {self.db}")
        con = sqlite3.connect(self.db, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def _load_village_cache(self):
        """Load all villages into memory once for fast fuzzy matching."""
        if self._village_cache is not None:
            return
        self._village_cache = {}
        try:
            con = self._conn()
            rows = con.execute(
                "SELECT village, district, state, layer_name FROM villages"
            ).fetchall()
            con.close()
            for r in rows:
                name = (r["village"] or "").strip().lower()
                if name:
                    # Keep all rows (same village name can appear in multiple districts)
                    self._village_cache.setdefault(name, []).append(dict(r))
            logger.info("[LocalityID] Village cache loaded: %d unique names",
                        len(self._village_cache))
        except Exception as exc:
            logger.warning("[LocalityID] Cache load failed: %s", exc)
            self._village_cache = {}

    def _load_admin_cache(self):
        """Load admin hierarchy rows once."""
        if self._admin_cache is not None:
            return
        self._admin_cache = []
        try:
            con = self._conn()
            rows = con.execute(
                "SELECT state, district, block, subdistrict FROM admin_hierarchy"
            ).fetchall()
            con.close()
            self._admin_cache = [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("[LocalityID] Admin cache load failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────

    def resolve_to_admin(self, verbatim: str, context_hint: str = "") -> dict:
        """
        Attempt to match verbatim locality text against the village index.
        context_hint is used to disambiguate identical village names across states.
        """
        if not verbatim:
            return {}
        self._load_village_cache()
        v_lower = verbatim.lower().strip()

        # Combine verbatim and context_hint so the disambiguator can see "Gujarat"
        disambig_ctx = (v_lower + " " + (context_hint or "")).lower()

        # 1. Exact match
        if v_lower in self._village_cache:
            rows = self._village_cache[v_lower]
            best = self._disambiguate_admin(rows, disambig_ctx)
            return {**best, "village": v_lower.title(), "match_score": 100}

        # 2. Fuzzy match
        if _RAPIDFUZZ and self._village_cache:
            keys = list(self._village_cache.keys())
            matches = process.extract(v_lower, keys, scorer=fuzz.token_set_ratio,
                                      score_cutoff=82, limit=5)
            if matches:
                best_key = matches[0][0]
                score    = matches[0][1]
                rows     = self._village_cache[best_key]
                best     = self._disambiguate_admin(rows, disambig_ctx)
                return {**best, "village": best_key.title(), "match_score": score}

        # 3. Token scan
        tokens = re.findall(r"[a-z]{3,}", v_lower)
        for tok in tokens:
            if tok in self._village_cache:
                rows = self._village_cache[tok]
                best = self._disambiguate_admin(rows, disambig_ctx)
                return {**best, "village": tok.title(), "match_score": 75}

        return {}

    def build_enriched_locality(self, verbatim: str, occ: dict = None) -> str:
        """Compose an enriched locality string for geocoding."""
        
        # Build a context string from the occurrence dict (habitat, comments, citation)
        ctx_parts = []
        if occ:
            for key in ("comments", "habitat", "Source Citation", "sourceCitation"):
                val = occ.get(key, "")
                if val: ctx_parts.append(str(val))
        context_hint = " ".join(ctx_parts)

        # Pass the context hint so the code can choose Gujarat over Assam
        admin = self.resolve_to_admin(verbatim, context_hint=context_hint)
        
        parts = [verbatim]
        if admin.get("district"):
            parts.append(f"{admin['district']} District")
        if admin.get("state"):
            parts.append(admin["state"])
            
        if len(parts) == 1 and occ:
            # Use occ dict fields as fallback
            for key in ("district", "stateProvince", "state", "administrativeArea"):
                v = (occ or {}).get(key, "")
                if v:
                    parts.append(v)
                    break
        return ", ".join(parts)

    def _disambiguate_admin(self, rows: List[dict], context: str) -> dict:
        """
        When multiple district rows share the same village name, pick the
        row whose state/district string appears in the context text.
        Falls back to the first row.
        """
        ctx = context.lower()
        for row in rows:
            if ((row.get("state") or "").lower() in ctx or
                    (row.get("district") or "").lower() in ctx):
                return row
        return rows[0]

    def _build_enriched_locality(rec: dict) -> str:
        """Thin wrapper — delegates to LocalityIdentifier (v6.0)."""
        
        # FIX: Map the LLM's regional context to the exact key the v6 Geocoder expects
        # This prevents 'Narara' from defaulting to Assam when the paper says Gujarat.
        if rec.get("primaryLocality"):
            rec["administrativeArea"] = rec["primaryLocality"]
        elif rec.get("habitat") or rec.get("comments"):
            # Fallback: dump habitat and comments into admin area so the geocoder can search it for states
            rec["administrativeArea"] = str(rec.get("habitat", "")) + " " + str(rec.get("comments", ""))

        return _locality_id.build_enriched_locality(
            rec.get("verbatimLocality", ""),
            rec   # occ dict used as fallback for district/state
        )

    def populate_locality_article(
        self, wiki, verbatim: str, sp_name: str, occ: dict, citation: str
    ):
        
    
        """
        Wrapper: calls wiki._update_locality_article() AND stores admin
        hierarchy metadata in the locality article JSON.
        """
        # Build context to prevent wrong-state assignment in the Wiki
        ctx = str(occ.get("comments", "")) + " " + citation
        admin = self.resolve_to_admin(verbatim, context_hint=ctx)
        slug  = wiki._slug(verbatim)
        art   = wiki._read("locality", slug) or {
            "title": verbatim, "type": "locality", "version": 1,
            "decimalLatitude": None, "decimalLongitude": None,
            "species_checklist": [], "habitat_types": [],
            "sections": {"overview": ""}, "provenance": [],
            "admin_hierarchy": {},
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        # Merge admin hierarchy
        if admin and not art.get("admin_hierarchy"):
            art["admin_hierarchy"] = {
                "state":        admin.get("state", ""),
                "district":     admin.get("district", ""),
                "block":        admin.get("block", ""),
                "subdistrict":  admin.get("subdistrict", ""),
                "village":      admin.get("village", ""),
                "match_score":  admin.get("match_score", 0),
            }

        # Update species checklist
        if sp_name and sp_name not in art.get("species_checklist", []):
            art.setdefault("species_checklist", []).append(sp_name)

        # Coordinates from occ if not yet set
        if not art.get("decimalLatitude") and occ.get("decimalLatitude"):
            art["decimalLatitude"]  = occ["decimalLatitude"]
            art["decimalLongitude"] = occ["decimalLongitude"]

        # Auto-populate overview section
        if not art.get("sections", {}).get("overview") and admin:
            state    = admin.get("state", "")
            district = admin.get("district", "")
            art.setdefault("sections", {})["overview"] = (
                f"{verbatim} is a locality"
                + (f" in {district} district" if district else "")
                + (f", {state}" if state else "")
                + "."
            )

        prov = {"citation": citation, "date": datetime.now().isoformat()}
        if not any(p.get("citation") == citation for p in art.get("provenance", [])):
            art.setdefault("provenance", []).append(prov)

        wiki._write("locality", slug, verbatim, art,
                    change_note=f"locality update with admin hierarchy: {sp_name}")


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — SQLITE SCHEMA & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

_WIKI_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    section      TEXT NOT NULL,
    slug         TEXT NOT NULL,
    title        TEXT NOT NULL,
    body_json    TEXT NOT NULL,
    version      INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(section, slug)
);
CREATE TABLE IF NOT EXISTS wiki_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id   INTEGER NOT NULL REFERENCES wiki_articles(id),
    version      INTEGER NOT NULL,
    body_json    TEXT NOT NULL,
    change_note  TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wiki_sec_slug ON wiki_articles(section, slug);
CREATE INDEX IF NOT EXISTS idx_wiki_ver_art  ON wiki_versions(article_id);
"""

# ── Temporal Augmented Retrieval helpers ──────────────────────────────────────
# ── Temporal Augmented Retrieval helpers ──────────────────────────────────────
_YEAR_RE = re.compile(r"\b(1[7-9]\d{2}|20[0-2]\d)\b")

# Authority / description-year suffixes that must be EXCLUDED from event year
# detection.  Pattern matches "(Forsskål, 1775)" / "(Author 1888)" etc.
_AUTHORITY_YEAR_RE = re.compile(
    r"\((?:[A-Z][A-Za-zÀ-ÿ'''\-]+(?:\s+(?:and|&|et|von|van|de)\s+)?)+,?\s*(1[7-9]\d{2}|20[0-2]\d)\)"
)


def _strip_authority_years(text: str) -> str:
    """Remove authority/description year expressions before year scanning."""
    return _AUTHORITY_YEAR_RE.sub("", str(text))


def _extract_year_from_text(
    text: str,
    strip_authority: bool = False,
) -> Optional[int]:
    """
    Extract the most semantically relevant year from a free-text string.

    Parameters
    ----------
    text : str
        Citation string, eventDate string, or free text.
    strip_authority : bool
        When True, authority-year patterns like "(Forsskål, 1775)" are removed
        before scanning so that description-year years are never returned as
        sampling/publication years.  Use True when operating on citation strings
        that may contain the taxonomic authority.
    """
    if not text:
        return None
    s = _strip_authority_years(str(text)) if strip_authority else str(text)
    hits = [int(y) for y in _YEAR_RE.findall(s)]
    if not hits:
        return None
    # Return the MAXIMUM year found — publication/sampling years are always
    # more recent than description years, so max is correct for provenance
    # tracking (min was the bug: it always returned the authority year).
    return max(hits)


def _extract_year_from_sampling(raw) -> Optional[int]:
    """
    Extract event year from a samplingEvent dict / JSON string / plain string.
    Strips authority years before scanning so '(Forsskål, 1775)' does not
    contaminate a primary collection record dated 2016.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return _extract_year_from_text(raw, strip_authority=True)
    if isinstance(raw, dict):
        d = (raw.get("date") or raw.get("eventDate")
             or raw.get("year") or raw.get("samplingYear") or "")
        return _extract_year_from_text(str(d), strip_authority=True)
    return None


def _extract_publication_year(citation: str) -> Optional[int]:
    """
    Extract the PUBLICATION year from a citation string.

    Strategy (in order):
      1. Year immediately after last author name group:
         "Prasade; Nagale; Apte 2016" or "Smith et al. (2023)"
      2. 4-digit year in parentheses: "(2016)"
      3. Any 4-digit year in the string after stripping authority patterns.

    Strictly avoids returning description / authority years that appear in the
    species name suffix  e.g. "(Forsskål, 1775)".
    """
    if not citation:
        return None
    s = _strip_authority_years(str(citation))

    # Pattern 1: year after dot/comma/semicolon/space following author names
    # handles "Prasade; Pooja Nagale; D. Apte 2016. Cassiopea..."
    m = re.search(r"(?:^|[;,.])\s*[\w.\-]+\s+(\d{4})(?=\b[^(])", s)
    if m:
        return int(m.group(1))

    # Pattern 2: standalone parenthesised year, e.g. "(2016)"
    m = re.search(r"\((\d{4})\)", s)
    if m:
        return int(m.group(1))

    # Pattern 3: last resort — any remaining year
    hits = [int(y) for y in _YEAR_RE.findall(s)]
    return max(hits) if hits else None

# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — LLM WIKI ARCHITECT PROMPTS  (unchanged from v5.5)
# ═════════════════════════════════════════════════════════════════════════════

_WIKI_ARCHITECT_SYSTEM = """\
You are a Professional Taxonomist and Wiki Editor specialising in marine
invertebrates, coastal ecology, and Indian Ocean biodiversity.

Your task: given (a) the CURRENT wiki article JSON and (b) NEW source text
(a PDF extract / chunk), produce an UPDATED article JSON that:

1. NEVER deletes existing valid data — only appends or refines.
2. Resolves conflicts by listing BOTH sources with inline citations, e.g.
   "Bhave (2011) reports 5 m; Smith (2024) reports 12 m."
3. Fills blank fields (authority, taxon rank, synonyms, depth, etc.)
   when the new text provides them.
4. Appends new localities, vouchers, and ecological notes.
5. Updates "sections" dict — each key maps to a markdown string.
   Use [cite:CANONICAL_KEY] markers when citing specific papers.
6. Uses italics (*Name*) for binomial nomenclature.
7. Uses **bold** for key technical terms.
8. Respects Wikipedia-style neutral tone.

─── CRITICAL: OCCURRENCE TYPE RULES ──────────────────────────────────────────
occurrence_role == "secondary":
  a) Do NOT populate morphology/diagnostic fields unless EXPLICITLY for THIS species.
  b) MAY update: distribution_habitat, specimen_records, taxonomy_phylogeny.
  c) Add note in lead: "*[Species]* is recorded here as a secondary occurrence."

Return ONLY valid JSON — no prose, no markdown fences.
"""

_WIKI_ARCHITECT_USER = """\
CURRENT ARTICLE JSON:
{current_json}

OCCURRENCE ROLE: {occurrence_role}
TARGET SPECIES (update ONLY): {sp_name}

NEW SOURCE TEXT:
{new_text}

PAPER CITATION: {citation}

Return only the updated JSON object.
"""


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — BLANK ARTICLE TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

def _blank_species_article(sp_name: str) -> dict:
    return {
        "title": sp_name, "type": "species", "version": 1,
        "kingdom": "", "phylum": "", "class_": "", "order_": "",
        "family_": "", "genus": "", "species_epithet": "",
        "authority": "", "taxonRank": "species",
        "taxonomicStatus": "unverified",
        "wormsID": "", "gbifID": "", "iucnStatus": "", "iucnURL": "",
        "synonyms": [],
        "coloration_life": "", "coloration_preserved": "",
        "body_length_mm": {"min": None, "max": None, "mean": None},
        "body_width_mm":  {"min": None, "max": None, "mean": None},
        "radular_formula": "", "diagnostic_characters": [],
        "diet": [], "depth_zone": "", "depth_range_raw": [],
        "substrate": [],
        "type_locality": {"verbatim": "", "latitude": None, "longitude": None, "source": ""},
        "occurrence_points": [], "habitats": [], "voucher_specimens": [],
        "collectors": [], "depth_conflicts": [], "size_conflicts": [],
        "temporal_index": {
            "earliest_primary_year": None, "earliest_secondary_year": None,
            "latest_year": None, "publication_years": [], "sampling_years": [],
            "wiki_created_year": None,
        },
        "sections": {
            "lead": "", "taxonomy_phylogeny": "", "morphology": "",
            "distribution_habitat": "", "ecology_behaviour": "",
            "conservation": "", "specimen_records": "",
        },
        # Scientific citation fields (v6.0)
        "bibliography": {},    # canonical_key → ref_dict
        "citation_index": {},  # canonical_key → int
        "provenance": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }


def _blank_locality_article(locality: str) -> dict:
    return {
        "title": locality, "type": "locality", "version": 1,
        "decimalLatitude": None, "decimalLongitude": None,
        "admin_hierarchy": {},         # v6.0 — from LocalityIdentifier
        "species_checklist": [],
        "habitat_types": [],
        "sections": {"overview": ""},
        "provenance": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — MAIN CLASS  BioTraceWikiUnified  (v6.0)
# ═════════════════════════════════════════════════════════════════════════════

class BioTraceWikiUnified(ScientificWikiCapability):
    """
    Unified, versioned, LLM-enhanceable wiki for BioTrace v6.0.

    Inherits ScientificWikiCapability → every instance can call:
        self.register_reference(art, ref_dict)
        self.rebuild_citation_index(art)
        self.render_bibliography_markdown(art)
        self.render_bibliography_html(art)
        self.to_bibtex(art)

    New in v6.0:
        self.locality_id  (LocalityIdentifier) — admin hierarchy lookups
        update_from_occurrences() → passes enriched locality to geocoding
        render_unified_page()    → bibliography injected at bottom of HTML
    """

    _MORPHO_FIELDS = frozenset({
        "coloration_life", "coloration_preserved", "body_length_mm",
        "body_width_mm", "radular_formula", "diagnostic_characters",
    })
    _MORPHO_SECTIONS = frozenset({"morphology"})

    # ── Construction ──────────────────────────────────────────────────────

    def __init__(
        self,
        root_dir: str,
        css_path: Optional[str] = None,
        hierarchy_db: str = "biodiversity_data/locality_hierarchy.db",
    ):
        self.root     = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path  = str(self.root / "wiki_unified.db")
        self.css_path = css_path
        self._init_db()

        # v6.0 — locality identifier
        self.locality_id = LocalityIdentifier(hierarchy_db=hierarchy_db)

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.executescript(_WIKI_SCHEMA)
        con.commit()
        con.close()

    # ── Slug ──────────────────────────────────────────────────────────────

    @staticmethod
    def _slug(text: str) -> str:
        text = str(text).lower().strip()
        text = re.sub(r"[^\w\s-]", "", text)
        return re.sub(r"[\s_]+", "-", text)

    # ── Read / Write ──────────────────────────────────────────────────────

    def _read(self, section: str, slug: str) -> Optional[dict]:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT id, body_json, version FROM wiki_articles WHERE section=? AND slug=?",
            (section, slug),
        ).fetchone()
        con.close()
        if not row: return None
        try:
            art = json.loads(row[1])
            art["_db_id"]  = row[0]
            art["version"] = row[2]
            return art
        except Exception:
            return None

    def _write(self, section: str, slug: str, title: str, art: dict,
               change_note: str = "") -> int:
        art["updated_at"] = datetime.now().isoformat()
        body_json = json.dumps(art, ensure_ascii=False)
        con = sqlite3.connect(self.db_path)
        existing = con.execute(
            "SELECT id, body_json, version FROM wiki_articles WHERE section=? AND slug=?",
            (section, slug),
        ).fetchone()
        if existing:
            art_id, old_json, old_ver = existing
            con.execute(
                "INSERT INTO wiki_versions (article_id, version, body_json, change_note) "
                "VALUES (?,?,?,?)", (art_id, old_ver, old_json, change_note),
            )
            new_ver = old_ver + 1
            art["version"] = new_ver
            body_json = json.dumps(art, ensure_ascii=False)
            con.execute(
                "UPDATE wiki_articles SET body_json=?, version=?, updated_at=datetime('now') "
                "WHERE id=?", (body_json, new_ver, art_id),
            )
        else:
            art["version"] = 1
            body_json = json.dumps(art, ensure_ascii=False)
            con.execute(
                "INSERT INTO wiki_articles (section, slug, title, body_json, version) "
                "VALUES (?,?,?,?,1)", (section, slug, title, body_json),
            )
            art_id = con.execute(
                "SELECT id FROM wiki_articles WHERE section=? AND slug=?",
                (section, slug),
            ).fetchone()[0]
        con.commit()
        con.close()
        return art_id

    # ── Version history & rollback ────────────────────────────────────────

    def list_versions(self, section: str, name: str) -> list:
        slug = self._slug(name)
        con  = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT wv.version, wv.change_note, wv.created_at "
            "FROM wiki_versions wv JOIN wiki_articles wa ON wa.id=wv.article_id "
            "WHERE wa.section=? AND wa.slug=? ORDER BY wv.version DESC",
            (section, slug),
        ).fetchall()
        con.close()
        return [{"version": r[0], "note": r[1], "date": r[2]} for r in rows]

    def rollback(self, section: str, name: str, to_version: int) -> bool:
        slug = self._slug(name)
        con  = sqlite3.connect(self.db_path)
        art_row = con.execute(
            "SELECT id, version FROM wiki_articles WHERE section=? AND slug=?",
            (section, slug),
        ).fetchone()
        if not art_row: con.close(); return False
        art_id, cur_ver = art_row
        snap = con.execute(
            "SELECT body_json FROM wiki_versions WHERE article_id=? AND version=?",
            (art_id, to_version),
        ).fetchone()
        if not snap: con.close(); return False
        cur_body = con.execute(
            "SELECT body_json FROM wiki_articles WHERE id=?", (art_id,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO wiki_versions (article_id, version, body_json, change_note) "
            "VALUES (?,?,?,?)",
            (art_id, cur_ver, cur_body, f"auto-snapshot before rollback to v{to_version}"),
        )
        con.execute(
            "UPDATE wiki_articles SET body_json=?, version=?, updated_at=datetime('now') "
            "WHERE id=?", (snap[0], cur_ver + 1, art_id),
        )
        con.commit(); con.close()
        return True

    # ── Public article accessors ───────────────────────────────────────────

    def get_species_article(self, sp_name: str) -> Optional[dict]:
        return self._read("species", self._slug(sp_name))

    def list_species(self) -> list:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT title FROM wiki_articles WHERE section='species' ORDER BY title"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]

    def list_localities(self) -> list:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT title FROM wiki_articles WHERE section='locality' ORDER BY title"
        ).fetchall()
        con.close()
        return [r[0] for r in rows]

    def index_stats(self) -> dict:
        con  = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT section, COUNT(*) FROM wiki_articles GROUP BY section"
        ).fetchall()
        con.close()
        by_sec = {r[0]: r[1] for r in rows}
        return {"total_articles": sum(by_sec.values()), "by_section": by_sec}

    # ── Reference management (v6.0 convenience wrappers) ──────────────────

    def add_reference_to_species(self, sp_name: str, ref_dict: dict):
        """Register a bibliography reference on a species article."""
        slug = self._slug(sp_name)
        art  = self._read("species", slug) or _blank_species_article(sp_name)
        self.register_reference(art, ref_dict)
        self.rebuild_citation_index(art)
        self._write("species", slug, sp_name, art,
                    change_note=f"reference added: {ref_dict.get('canonical_key','')}")

    # ── Ingestion ──────────────────────────────────────────────────────────

    def update_from_occurrences(
        self,
        occurrences:       list,
        citation:          str = "",
        llm_fn:            Optional[Callable] = None,
        update_narratives: bool = False,
        chunk_text:        str = "",
        extra_facts_map:   Optional[dict] = None,
        references:        Optional[list] = None,    # v6.0: bibliography
    ) -> dict:
        """
        Ingest occurrence dicts.
        v6.0 additions:
          • references list → auto-registered on each species article
          • locality_id enriches verbatimLocality before updating locality articles
        """
        counts = {"species": 0, "locality": 0}
        extra_facts_map = extra_facts_map or {}
        enhanced_species: set = set()

        for occ in occurrences:
            sp_name = occ.get("validName") or occ.get("recordedName", "")
            if not sp_name:
                continue
            self._update_species_article(sp_name, occ, citation,
                                         extra_facts=extra_facts_map.get(sp_name, {}))
            counts["species"] += 1
            enhanced_species.add(sp_name)

            # v6.0 — register bibliography references on article
            if references:
                slug = self._slug(sp_name)
                art  = self._read("species", slug) or _blank_species_article(sp_name)
                for ref in references:
                    rd = ref if isinstance(ref, dict) else (
                        {k: getattr(ref, k, None) for k in
                         ["canonical_key","authors","year","title","journal_name",
                          "volume","issue","pages","doi","source_type","publisher"]}
                    )
                    self.register_reference(art, rd)
                self.rebuild_citation_index(art)
                self._write("species", slug, sp_name, art,
                            change_note=f"bibliography updated: {citation[:50]}")

            loc = occ.get("verbatimLocality", "")
            if loc and loc.lower() not in ("not reported", "unknown", ""):
                # v6.0 — use LocalityIdentifier for enriched locality articles
                self.locality_id.populate_locality_article(
                    self, loc, sp_name, occ, citation
                )
                counts["locality"] += 1

        if update_narratives and llm_fn and chunk_text.strip():
            for sp_name in enhanced_species:
                try:
                    self._enhance_with_llm(sp_name, chunk_text, citation, llm_fn)
                    counts["llm_enhanced"] = counts.get("llm_enhanced", 0) + 1
                except Exception as exc:
                    logger.warning("[Wiki] LLM enhance failed for %s: %s", sp_name, exc)

        return counts

    def _update_species_article(self, sp_name, occ, citation, extra_facts=None):
        slug = self._slug(sp_name)
        art  = self._read("species", slug) or _blank_species_article(sp_name)

        def _fill(art_key, *occ_keys):
            if not art.get(art_key):
                for k in occ_keys:
                    v = occ.get(k, "")
                    if v and str(v).strip():
                        art[art_key] = str(v).strip(); break

        _fill("phylum",          "phylum", "Phylum")
        _fill("class_",          "class_", "Class")
        _fill("order_",          "order_", "Order")
        _fill("family_",         "family_", "Family")
        _fill("wormsID",         "wormsID")
        _fill("gbifID",          "gbifID")
        _fill("taxonRank",       "taxonRank")
        _fill("taxonomicStatus", "taxonomicStatus")

        if not art.get("authority"):
            auth = (occ.get("nameAccordingTo") or occ.get("authority") or "")
            if auth: art["authority"] = str(auth).strip()

        _fill("iucnStatus", "iucnStatus", "iucn_status")

        # lat = occ.get("decimalLatitude"); lon = occ.get("decimalLongitude")
        # loc = occ.get("verbatimLocality", "")
        # occ_pt = {
        #     "locality": loc, "latitude": lat, "longitude": lon,
        #     "depth_m": None, "source": citation,
        #     "occurrenceType": occ.get("occurrenceType", "Uncertain"),
        #     "samplingEvent": None,
        # }
        # se = occ.get("samplingEvent") or {}
        # if isinstance(se, str):
        #     try: se = json.loads(se)
        #     except: se = {}
        # if isinstance(se, dict):
        #     if se.get("depth_m"):
        #         try: occ_pt["depth_m"] = float(se["depth_m"])
        #         except: pass
        #     occ_pt["samplingEvent"] = se
        
        lat = occ.get("decimalLatitude"); lon = occ.get("decimalLongitude")
        loc = (occ.get("verbatimLocality") or occ.get("locality") or "")

        # ── Resolve samplingEvent ─────────────────────────────────────────────
        se = occ.get("samplingEvent") or occ.get("Sampling Event") or {}
        if isinstance(se, str):
            try:
                se = json.loads(se)
            except Exception:
                se = {}
        if not isinstance(se, dict):
            se = {}

        # ── Determine event year (LLM-extracted eventDate takes priority) ────
        #   1. Explicit eventDate on the occurrence dict (set by BioCentricExtractor)
        #   2. samplingEvent.date / .eventDate / .year
        #   3. Fallback: publication year derived from the citation string
        event_year: Optional[int] = None

        raw_date = (
            occ.get("eventDate") or occ.get("event_date")
            or se.get("date") or se.get("eventDate") or se.get("year") or ""
        )
        if raw_date:
            event_year = _extract_year_from_text(str(raw_date), strip_authority=True)

        if event_year is None and citation:
            # No explicit event date → use publication year as the sampling year.
            # This is semantically correct: if methodology does not report a
            # separate collection year, the publication year is the best proxy.
            event_year = _extract_publication_year(citation)

        # Write year back into samplingEvent so render_occurrence_table_html
        # picks it up without further changes to the renderer.
        if event_year is not None:
            se["year"] = event_year

        occ_pt = {
            "locality":       loc,
            "latitude":       lat,
            "longitude":      lon,
            "depth_m":        None,
            "source":         citation,
            "occurrenceType": (occ.get("occurrenceType") or occ.get("occurrence_type") or "Uncertain"),
            "recordedName":   occ.get("recordedName", ""),
            "samplingEvent":  se if se else None,
        }

        if se.get("depth_m"):
            try:
                occ_pt["depth_m"] = float(se["depth_m"])
            except (TypeError, ValueError):
                pass

        _hash = hashlib.md5(f"{loc}_{citation}".encode()).hexdigest()[:8]
        existing_hashes = [
            hashlib.md5(f"{p['locality']}_{p['source']}".encode()).hexdigest()[:8]
            for p in art["occurrence_points"]
        ]
        if _hash not in existing_hashes and (lat or loc):
            art["occurrence_points"].append(occ_pt)

        hab = occ.get("habitat", "")
        if hab and hab not in art["habitats"]: art["habitats"].append(hab)
        if extra_facts: self._merge_extra_facts(art, extra_facts)

        prov_entry = {"citation": citation, "date": datetime.now().isoformat()}
        if not any(p["citation"] == citation for p in art["provenance"]):
            art["provenance"].append(prov_entry)

        if not art.get("sections", {}).get("lead", "").strip():
            art.setdefault("sections", {})["lead"] = self._autostub_lead(art)

        self._write("species", slug, sp_name, art,
                    change_note=f"occurrence ingest: {citation}")
        try: self.refresh_temporal_index(sp_name)
        except Exception: pass

    @staticmethod
    def _autostub_lead(art: dict) -> str:
        sp  = art.get("title", "This species")
        fam = art.get("family_", ""); order = art.get("order_", "")
        tax = (f"family *{fam}*" if fam else "") or (f"order *{order}*" if order else "an unresolved group")
        pts   = art.get("occurrence_points", [])
        locs  = sorted({p["locality"] for p in pts if p.get("locality") and p["locality"] != "—"})
        depths = [p["depth_m"] for p in pts if p.get("depth_m") is not None]
        loc_str   = ", ".join(locs[:3]) + ("…" if len(locs) > 3 else "") if locs else ""
        depth_str = ""
        if depths:
            dmin, dmax = min(depths), max(depths)
            depth_str = (f" at depths of {dmin:.0f}–{dmax:.0f} m"
                         if dmin != dmax else f" at approximately {dmin:.0f} m depth")
        occ_s = f" It has been recorded from {loc_str}{depth_str}." if loc_str else ""
        iucn  = art.get("iucnStatus", "")
        iucn_s = f" Its IUCN status is **{iucn}**." if iucn else ""
        return (f"*{sp}* is a species belonging to the {tax}.{occ_s}{iucn_s}"
                " *(Auto-generated stub — awaiting literature enhancement pass.)*")

    @staticmethod
    def _merge_extra_facts(art: dict, extra: dict):
        for k, v in extra.items():
            if not v: continue
            if k not in art: art[k] = v
            elif isinstance(art[k], list) and isinstance(v, list):
                for item in v:
                    if item not in art[k]: art[k].append(item)
            elif isinstance(art[k], dict) and isinstance(v, dict):
                for dk, dv in v.items():
                    if not art[k].get(dk) and dv: art[k][dk] = dv
            elif not art[k] and v: art[k] = v

    # ── Temporal Augmented Retrieval (TAR) ───────────────────────────────

    def refresh_temporal_index(self, sp_name: str, meta_db: str = "") -> dict:
        slug = self._slug(sp_name)
        art  = self._read("species", slug)
        if not art: return {}
        ti: dict = {
            "earliest_primary_year": None, "earliest_secondary_year": None,
            "latest_year": None, "publication_years": [], "sampling_years": [],
            "wiki_created_year": _extract_year_from_text(art.get("created_at", "")),
        }
        # for prov in art.get("provenance", []):
        #     cit  = prov.get("citation", ""); role = prov.get("occurrence_role", "primary")
        #     year = _extract_year_from_text(cit)
        #     if year:
        #         entry = {"year": year, "role": role, "citation": cit[:120]}
        #         if not any(e["year"]==year and e["citation"]==entry["citation"]
        #                    for e in ti["publication_years"]):
        #             ti["publication_years"].append(entry)
        
        for prov in art.get("provenance", []):
            cit  = prov.get("citation", "")
            role = prov.get("occurrence_role", "primary")
            # Use _extract_publication_year so authority years in the citation
            # string (e.g. "Cassiopea andromeda (Forsskål, 1775)") are stripped
            # before scanning — preventing 1775 from appearing as primary year.
            year = _extract_publication_year(cit) if cit else None
            if year:
                entry = {"year": year, "role": role, "citation": cit}
                if not any(
                    e["year"] == year and e["citation"] == entry["citation"]
                    for e in ti["publication_years"]
                ):
                    ti["publication_years"].append(entry)
                    
                    
        for pt in art.get("occurrence_points", []):
            role_key = "secondary" if (pt.get("occurrenceType") or "").lower() == "secondary" else "primary"
            # year = _extract_year_from_sampling(pt.get("samplingEvent")) or \
            #        _extract_year_from_text(pt.get("source", ""))
            
            year = (
                _extract_year_from_sampling(pt.get("samplingEvent"))
                or _extract_publication_year(pt.get("source", ""))
            )
            
            if year:
                loc   = pt.get("locality", "")
                entry = {"year": year, "role": role_key, "locality": loc[:80]}
                if not any(e["year"]==year and e["locality"]==entry["locality"]
                           for e in ti["sampling_years"]):
                    ti["sampling_years"].append(entry)
        by_role: Dict[str, list] = {"primary": [], "secondary": []}
        for e in ti["publication_years"] + ti["sampling_years"]:
            by_role.setdefault(e.get("role","primary"), []).append(e["year"])
        if by_role["primary"]:   ti["earliest_primary_year"]   = min(by_role["primary"])
        if by_role["secondary"]: ti["earliest_secondary_year"] = min(by_role["secondary"])
        all_y = by_role["primary"] + by_role["secondary"]
        if all_y: ti["latest_year"] = max(all_y)
        ti["publication_years"].sort(key=lambda e: e["year"])
        ti["sampling_years"].sort(key=lambda e: e["year"])
        art["temporal_index"] = ti
        self._write("species", slug, sp_name, art, change_note="TAR: temporal index refreshed")
        return ti

    def build_temporal_index(self, sp_name: str, meta_db: str = "") -> dict:
        art = self.get_species_article(sp_name) or {}
        ti  = art.get("temporal_index", {})
        if ti and ti.get("latest_year"): return ti
        return self.refresh_temporal_index(sp_name, meta_db=meta_db)

    def get_species_timeline(self, meta_db: str = "", role_filter: Optional[str] = None) -> list:
        timeline = []
        for sp_name in self.list_species():
            art = self.get_species_article(sp_name) or {}
            ti  = art.get("temporal_index", {})
            if not ti or not ti.get("latest_year"):
                ti = self.refresh_temporal_index(sp_name, meta_db=meta_db)
            for e in ti.get("sampling_years", []):
                if role_filter and e.get("role") != role_filter: continue
                timeline.append({"species": sp_name, "year": e["year"],
                                 "role": e.get("role","primary"),
                                 "evidence_type": "sampling",
                                 "locality": e.get("locality",""), "citation": ""})
            for e in ti.get("publication_years", []):
                if role_filter and e.get("role") != role_filter: continue
                timeline.append({"species": sp_name, "year": e["year"],
                                 "role": e.get("role","primary"),
                                 "evidence_type": "publication",
                                 "locality": "", "citation": e.get("citation","")})
        timeline.sort(key=lambda x: x["year"])
        return timeline

    def build_wiki_context(
        self, query: str, top_k: int = 5, meta_db: str = "",
        year_bias: Optional[int] = None, year_window: int = 10,
        role_filter: Optional[str] = None,
    ) -> str:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT title, body_json FROM wiki_articles WHERE section='species' ORDER BY updated_at DESC"
        ).fetchall()
        con.close()
        scored = []
        for title, body_json in rows:
            try:
                art  = json.loads(body_json)
                ti   = art.get("temporal_index", {})
                lead = art.get("sections", {}).get("lead", "")[:400]
                if role_filter == "primary" and not ti.get("earliest_primary_year"): continue
                if role_filter == "secondary" and not ti.get("earliest_secondary_year"): continue
                if year_bias is not None:
                    art_year = (ti.get("earliest_primary_year") or
                                ti.get("earliest_secondary_year") or ti.get("latest_year"))
                    score = (1.0 / (1.0 + abs(art_year - year_bias))) if art_year else 0.01
                else:
                    score = float(ti.get("latest_year") or 0) or 0.5
                scored.append((score, title, lead))
            except Exception: pass
        scored.sort(key=lambda x: -x[0])
        return "\n\n".join(f"=={t}==\n{l}" for _, t, l in scored[:top_k])

    # ── LLM Wiki Architect enhancement ────────────────────────────────────

    def _derive_occurrence_role(self, art: dict) -> str:
        pts = art.get("occurrence_points", [])
        if not pts: return "primary"
        for pt in pts:
            ot = (pt.get("occurrenceType") or "").strip().lower()
            if ot in ("primary", ""): return "primary"
        return "secondary"

    def _enhance_with_llm(self, sp_name, chunk_text, citation, llm_fn):
        slug = self._slug(sp_name)
        art  = self._read("species", slug)
        if not art: return
        chunk_hash = hashlib.md5(chunk_text[:2000].encode()).hexdigest()[:12]
        if any(p.get("chunk_hash") == chunk_hash for p in art.get("provenance", [])):
            logger.debug("[Wiki] chunk already processed for %s", sp_name); return

        occurrence_role = self._derive_occurrence_role(art)
        art_for_prompt  = {k: v for k, v in art.items()
                           if k not in ("_db_id","provenance","occurrence_points",
                                        "bibliography","citation_index")}
        art_for_prompt["occurrence_count"] = len(art.get("occurrence_points", []))
        art_for_prompt["occurrence_role"]  = occurrence_role

        prompt = (_WIKI_ARCHITECT_SYSTEM + "\n\n" +
                  _WIKI_ARCHITECT_USER.format(
                      current_json=json.dumps(art_for_prompt, indent=2)[:6000],
                      new_text=chunk_text[:4000], citation=citation,
                      occurrence_role=occurrence_role, sp_name=sp_name,
                  ))
        raw = llm_fn(prompt)
        raw = re.sub(r"^```+(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r"\s*```+$", "", raw.strip(), flags=re.MULTILINE)

        try:
            updated = json.loads(raw)
        except Exception as exc:
            logger.warning("[Wiki] LLM JSON parse failed for %s: %s", sp_name, exc); return

        if occurrence_role == "secondary":
            for bad_key in self._MORPHO_FIELDS: updated.pop(bad_key, None)
            updated.get("sections", {}).pop("morphology", None)

        for k, v in updated.items():
            if k in ("_db_id","version","created_at","occurrence_role",
                     "bibliography","citation_index"): continue
            if isinstance(v, str) and v.strip():
                if not art.get(k): art[k] = v
            elif isinstance(v, list) and v:
                if isinstance(art.get(k), list): self._merge_extra_facts(art, {k: v})
                else: art[k] = v
            elif isinstance(v, dict): self._merge_extra_facts(art, {k: v})

        updated_secs = updated.get("sections", {})
        for sec_key, new_text in updated_secs.items():
            if not new_text: continue
            existing = art.get("sections", {}).get(sec_key, "")
            if not existing: art.setdefault("sections", {})[sec_key] = new_text
            elif new_text.strip() and new_text.strip() not in existing:
                art["sections"][sec_key] = existing.rstrip() + "\n\n" + new_text.strip()

        # Rebuild citation index to include any [cite:KEY] markers added by LLM
        self.rebuild_citation_index(art)

        art.setdefault("provenance", []).append({
            "citation": citation, "date": datetime.now().isoformat(),
            "chunk_hash": chunk_hash, "enhanced": True,
            "occurrence_role": occurrence_role,
        })
        self._write("species", slug, sp_name, art,
                    change_note=f"LLM-enhanced ({occurrence_role}) chunk {chunk_hash}: {citation[:50]}")
        try: self.refresh_temporal_index(sp_name)
        except Exception: pass

    # ── Rendering ─────────────────────────────────────────────────────────

    @staticmethod
    def _iucn_badge_class(status: str) -> str:
        return {"LC":"wiki-badge-iucn-lc","NT":"wiki-badge-iucn-nt",
                "VU":"wiki-badge-iucn-vu","EN":"wiki-badge-iucn-en",
                "CR":"wiki-badge-iucn-cr","DD":"wiki-badge-iucn-dd",
                }.get((status or "").upper(), "wiki-badge-rank")

    @staticmethod
    def _status_badge_class(status: str) -> str:
        s = (status or "").lower()
        if s in ("accepted","verified","accept"): return "wiki-badge-verified"
        if s in ("rejected","reject"):            return "wiki-badge-rejected"
        return "wiki-badge-unverified"

    def render_taxobox_html(self, art: dict) -> str:
        sp_name   = art.get("title", "")
        authority = art.get("authority", "")
        lineage   = " &rsaquo; ".join(
            r for r in [art.get("kingdom","Animalia"), art.get("phylum",""),
                        art.get("class_",""), art.get("order_",""), art.get("family_","")] if r
        )
        rows = [
            ("Kingdom", art.get("kingdom","") or "Animalia"),
            ("Phylum",  art.get("phylum","")),
            ("Class",   art.get("class_","")),
            ("Order",   art.get("order_","")),
            ("Family",  art.get("family_","")),
            ("Genus",   art.get("genus","") or (sp_name.split()[0] if sp_name else "")),
            ("Species", f"<i>{sp_name}</i>" if sp_name else ""),
            ("Authority", authority or "<span style='color:#555'>—</span>"),
        ]
        row_html = "".join(
            f"<tr><td>{lbl}</td><td{' class=\"no-italic\"' if lbl in ('Kingdom','Authority','Family','Order','Class','Phylum') else ''}>{val}</td></tr>"
            for lbl, val in rows if val
        )
        wid = art.get("wormsID", ""); gbif = art.get("gbifID", "")
        footer = ""
        if wid:
            footer += (f'<div class="wiki-taxobox-footer"><a href="https://www.marinespecies.org/'
                       f'aphia.php?p=taxdetails&id={wid}" target="_blank">🔗 WoRMS AphiaID {wid}</a></div>')
        if gbif:
            footer += (f'<div class="wiki-taxobox-footer"><a href="https://www.gbif.org/species/{gbif}" '
                       f'target="_blank">🔗 GBIF {gbif}</a></div>')
        return f"""
        <div class="wiki-taxobox">
          <div class="wiki-taxobox-header">Scientific Classification</div>
          <div class="wiki-taxobox-species-name"><i>{sp_name}</i></div>
          <div class="lineage" style="text-align:center;font-size:0.85em;padding:4px 10px;color:#ddd">{lineage}</div>
          <table>{row_html}</table>
          {footer}
        </div>"""

    def render_badge_row_html(self, art: dict) -> str:
        status = art.get("taxonomicStatus","unverified"); rank = art.get("taxonRank","species")
        iucn   = art.get("iucnStatus",""); auth = art.get("authority","")
        badges = [
            f'<span class="wiki-badge {self._status_badge_class(status)}">● {status}</span>',
            f'<span class="wiki-badge wiki-badge-rank">Rank: {rank}</span>',
        ]
        if auth:  badges.append(f'<span class="wiki-badge wiki-badge-rank">Authority: {auth}</span>')
        if iucn:  badges.append(f'<span class="wiki-badge {self._iucn_badge_class(iucn)}">IUCN {iucn}</span>')
        ver = art.get("version",1); upd = art.get("updated_at","")[:10]
        badges.append(f'<span class="wiki-version-chip">v{ver} · {upd}</span>')
        return f'<div class="wiki-badge-row">{"".join(badges)}</div>'

    def render_occurrence_table_html(self, sp_name: str, meta_db: str = "", max_rows: int = 100) -> str:
        """Render the documented occurrences table. Live DB preferred."""
        art = self.get_species_article(sp_name) or {}
        pts = art.get("occurrence_points", [])[:max_rows]
        if not pts: return ""
        ot_cls = {"Primary":"occ-primary","Secondary":"occ-secondary"}
        rows_html = ""
        for i, pt in enumerate(pts, 1):
            lat = pt.get("latitude"); lon = pt.get("longitude")
            coord = (f"{lat:.4f}, {lon:.4f}" if (lat is not None and lon is not None) else "—")
            loc  = pt.get("locality") or "—"
            ot   = str(pt.get("occurrenceType","Uncertain"))
            cls  = ot_cls.get(ot,"occ-uncertain")
            src  = str(pt.get("source",""))
            samp = pt.get("samplingEvent") or {}
            if isinstance(samp, str):
                try: samp = json.loads(samp)
                except: samp = {}
            # yr   = str(samp.get("year","") or samp.get("date","")[:4] if samp else "")
            
            yr_raw = (
                samp.get("year")
                or samp.get("eventDate")
                or samp.get("date")
            ) if samp else None
            yr = str(yr_raw)[:4] if yr_raw else ""
            
            
            dep = pt.get("depth_m") or (samp.get("depth_m") if samp else None)
            
            try: 
                dep_s = f"{float(dep):.0f}"
            except (ValueError, TypeError): 
                dep_s = "—"
            
            
            rows_html += (f"<tr><td style='text-align:center;color:#888'>{i}</td>"
                          f"<td>{loc[:50]}</td><td class='{cls}'>{ot}</td>"
                          f"<td style='text-align:center'><b>{yr or '—'}</b></td>"
                          f"<td style='text-align:center'>{dep_s}</td>"
                          f"<td style='font-size:0.82em'>{coord}</td>"
                          f"<td style='font-size:0.78em' title='{src}'>{src[:50]}{'…' if len(src)>50 else ''}</td></tr>")
        return (f"<h2 class='wiki-section-h2'>📋 Documented Occurrences</h2>"
                f"<div class='wiki-occ-table-wrap'><table class='wiki-occ-table'>"
                f"<thead><tr><th>#</th><th>Locality</th><th>Type</th><th>Year</th>"
                f"<th>Depth (m)</th><th>Coordinates</th><th>Source</th></tr></thead>"
                f"<tbody>{rows_html}</tbody></table></div>")

    def render_unified_page(self, sp_name: str, meta_db: str = "") -> str:
        """
        Render full Wikipedia-style HTML page.
        v6.0: bibliography appended at bottom with clickable footnote links.
        """
        art = self.get_species_article(sp_name)
        if not art:
            return f"<p>No wiki article found for <i>{sp_name}</i>.</p>"

        css  = self._load_css()
        secs = art.get("sections", {})
        sp   = art.get("title", sp_name)
        idx  = art.get("citation_index", {})

        lead_text = secs.get("lead","").strip() or self._autostub_lead(art)

        ti = art.get("temporal_index", {})

        # temporal_badge = ""
        # if ep or es or ly:
        #     parts = []
        #     if ep: parts.append(f"📌 Primary from <b>{ep}</b>")
        #     if es: parts.append(f"📎 Secondary from <b>{es}</b>")
        #     if ly: parts.append(f"🕒 Latest <b>{ly}</b>")
        #     temporal_badge = (f'<div class="wiki-temporal-badge" '
        #                       f'style="font-size:0.82em;color:#bbb;padding:4px 0 8px 0">'
        #                       f'{" &nbsp;·&nbsp; ".join(parts)}</div>')



        # Fetch and immediately filter out non-numeric placeholder strings
        ep = ti.get("earliest_primary_year")
        es = ti.get("earliest_secondary_year")
        ly = ti.get("latest_year")

        # Ensure placeholders are treated as None so they don't trigger the 'if' blocks
        invalid_vals = (None, "", "—", "Not Reported")
        ep = None if ep in invalid_vals else ep
        es = None if es in invalid_vals else es
        ly = None if ly in invalid_vals else ly

        temporal_badge = ""
        if ep or es or ly:
            parts = []
            # If formatting floats as integers is needed, use f"{float(ep):.0f}" safely here
            if ep: parts.append(f"📌 Primary from <b>{ep}</b>")
            if es: parts.append(f"📎 Secondary from <b>{es}</b>")
            if ly: parts.append(f"🕒 Latest <b>{ly}</b>")
            temporal_badge = (f'<div class="wiki-temporal-badge" '
                            f'style="font-size:0.82em;color:#bbb;padding:4px 0 8px 0">'
                            f'{" &nbsp;·&nbsp; ".join(parts)}</div>')
        occ_table = self.render_occurrence_table_html(sp_name, meta_db=meta_db)
        diag_html = ""
        diags = art.get("diagnostic_characters", [])
        if diags:
            items = "".join(f"<li>{d}</li>" for d in diags[:10])
            diag_html = f'<ul class="wiki-diag-list">{items}</ul>'

        conflicts_html = ""
        for cf in art.get("depth_conflicts",[]) + art.get("size_conflicts",[]):
            note = "; ".join(cf.get("sources",[]))
            if note: conflicts_html += f'<div class="wiki-conflict">{note}</div>'

        prov_items = "".join(
            f"<li>{p.get('citation','')[:80]} <em>({p.get('date','')[:10]})"
            f"{'✨' if p.get('enhanced') else ''}"
            f"{'['+p.get('occurrence_role','')+']' if p.get('occurrence_role') else ''}"
            f"</em></li>"
            for p in art.get("provenance", [])
        )
        prov_html = f'<div class="wiki-references"><ol>{prov_items}</ol></div>' if prov_items else ""

        # v6.0 — bibliography with clickable footnotes
        bib_html = self.render_bibliography_html(art)

        def sec(key, heading, icon=""):
            txt = secs.get(key, "")
            if not txt: return ""
            rendered = self.render_section_html_with_citations(txt, idx)
            return f'<h2 class="wiki-section-h2">{icon} {heading}</h2><p>{rendered}</p>'

        # Admin hierarchy badge for locality context
        admin_badge = ""
        occ_pts = art.get("occurrence_points", [])
        # (locality admin info lives in locality articles, not species — shown in locality tab)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{sp} — BioTrace Wiki</title>
<style>{css}</style>
</head>
<body>
<div class="wiki-page">
  <h1 class="wiki-title"><i>{sp}</i></h1>
  <p class="wiki-subtitle">BioTrace Living Knowledge Base · Auto-generated from primary literature</p>
  {self.render_badge_row_html(art)}
  {temporal_badge}
  <div class="wiki-lead">
    {self.render_taxobox_html(art)}
    <p>{self.render_section_html_with_citations(lead_text, idx)}</p>
    {diag_html}
    {conflicts_html}
  </div>
  {sec('taxonomy_phylogeny',   'Taxonomy & Phylogeny',        '🔬')}
  {sec('morphology',           'Anatomy & Morphology',        '🔭')}
  {sec('distribution_habitat', 'Distribution & Habitat',      '🌍')}
  {sec('ecology_behaviour',    'Ecology & Behaviour',         '🐟')}
  {sec('conservation',         'Conservation Status',         '🛡️')}
  {sec('specimen_records',     'Specimen Records',            '🏛️')}
  {occ_table}
  {bib_html}
  <h2 class="wiki-section-h2">📚 Source Provenance</h2>
  {prov_html}
</div>
</body>
</html>"""

    def _load_css(self) -> str:
        candidates = [
            self.css_path,
            Path(__file__).with_name("biotrace_wiki.css"),
            Path("biotrace_wiki.css"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return Path(c).read_text(encoding="utf-8")
        return ""

    def render_species_markdown(self, sp_name: str) -> str:
        """Backward-compatible markdown render with bibliography."""
        art = self.get_species_article(sp_name)
        if not art: return f"*Article not found for {sp_name}.*"
        sp = art.get("title", sp_name); auth = art.get("authority","—")
        phylum = art.get("phylum",""); class_ = art.get("class_","")
        order_ = art.get("order_",""); family_ = art.get("family_","")
        status = art.get("taxonomicStatus","unverified")
        rank   = art.get("taxonRank","species"); iucn = art.get("iucnStatus","")
        wid    = art.get("wormsID","")
        idx    = art.get("citation_index", {})
        lines  = [f"# *{sp}*", "",
                  f"| **Family:** {family_} | **Order:** {order_} | **Phylum:** {phylum} |",
                  "|:---|:---|:---|",
                  f"| **Status:** {status} | **Rank:** {rank} | **Authority:** {auth} |", ""]
        if iucn: lines += [f"> **IUCN Status:** {iucn}", ""]
        if wid:  lines += [f"[🔗 WoRMS AphiaID {wid}](https://www.marinespecies.org/aphia.php?p=taxdetails&id={wid})", ""]
        secs = art.get("sections", {})
        for key, heading in [
            ("lead","## Overview"), ("taxonomy_phylogeny","## Taxonomy & Phylogeny"),
            ("morphology","## Anatomy & Morphology"), ("distribution_habitat","## Distribution & Habitat"),
            ("ecology_behaviour","## Ecology & Behaviour"), ("conservation","## Conservation"),
            ("specimen_records","## Specimen Records"),
        ]:
            txt = secs.get(key,"")
            if txt:
                resolved = self._resolve_citations(txt, idx)
                lines += [heading, "", resolved, ""]
        occ_pts = art.get("occurrence_points", [])
        if occ_pts:
            lines += ["## Documented Occurrences", ""]
            for pt in occ_pts[:20]:
                loc = pt.get("locality") or "—"
                ot  = pt.get("occurrenceType","?")
                src = str(pt.get("source",""))
                dep = pt.get("depth_m")
                try: dep_s = f" · {float(dep):.0f} m depth" if dep not in (None,"","—","None") else ""
                except: dep_s = ""
                lines.append(f"- **{loc}** ({ot}{dep_s}) — _{src}_")
            lines.append("")
        # v6.0 — bibliography
        lines.append(self.render_bibliography_markdown(art))
        return "\n".join(lines)

    # ── Streamlit Tab ──────────────────────────────────────────────────────

    def render_streamlit_tab(
        self, provider="", model_sel="", api_key="",
        ollama_url="http://localhost:11434", meta_db="",
        call_llm_fn: Optional[Callable] = None,
    ):
        if not _ST: return
        if self.css_path:
            css = self._load_css()
            if css: st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)

        st.subheader("📖 Wiki — Living Knowledge Base")
        st.caption("Wikipedia-style · LLM-enhanced · Git-versioned · Scientific bibliography")

        idx_  = self.index_stats()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Articles", idx_["total_articles"])
        c2.metric("Species",    idx_["by_section"].get("species",  0))
        c3.metric("Localities", idx_["by_section"].get("locality", 0))
        c4.metric("Saved Versions", self._count_versions())
        st.divider()

        sp_list = self.list_species()
        if not sp_list:
            st.info("No wiki articles yet. Run an extraction to populate."); return

        def _strip_auth(name):
            return re.sub(
                r"\s+[A-Z][A-Za-z\-'']+(?:\s+(?:and|&|et)\s+[A-Z][A-Za-z\-'']+)?[,.]?\s*\d{4}.*$",
                "", name
            ).strip()

        display_map = {_strip_auth(s): s for s in sp_list}
        selected_display = st.selectbox("View Species Article:", sorted(display_map.keys()),
                                        key="wiki_unified_sp_sel")
        selected_sp = display_map.get(selected_display, selected_display)
        if not selected_sp: return
        art = self.get_species_article(selected_sp) or {}

        view_tab, bib_tab, raw_tab, ver_tab = st.tabs(
            ["📄 Wiki Page", "📚 Bibliography", "🗂️ Raw Article", "📜 Version History"]
        )

        with view_tab:
            
            # --- NEW GBIF AUTOMATED VERIFICATION BUTTON ---
            if st.button("🌿 Auto-Verify via GBIF Backbone", key="wiki_gbif_verify"):
                with st.spinner("Querying GBIF API..."):
                    import requests, urllib.parse
                    url = f"https://api.gbif.org/v1/species/match?name={urllib.parse.quote(selected_sp)}&verbose=true"
                    try:
                        resp = requests.get(url, timeout=10).json()
                        if resp.get("matchType") != "NONE":
                            # Auto-save the GBIF ID and Status to the Wiki Article
                            art["gbifID"] = str(resp.get("usageKey", ""))
                            art["taxonomicStatus"] = resp.get("status", "accepted").lower()
                            art["taxonRank"] = resp.get("rank", "species").lower()
                            if resp.get("kingdom"): art["kingdom"] = resp.get("kingdom")
                            if resp.get("phylum"): art["phylum"] = resp.get("phylum")
                            if resp.get("class"): art["class_"] = resp.get("class")
                            if resp.get("order"): art["order_"] = resp.get("order")
                            if resp.get("family"): art["family_"] = resp.get("family")
                            
                            self._write("species", self._slug(selected_sp), selected_sp, art, "GBIF Auto-Verify")
                            
                            st.success(f"✅ **GBIF Match Found!** Updated Wiki classification to: *{resp.get('scientificName')}* ({resp.get('status')}).")
                            st.rerun() # Refresh page to show updated taxobox badges
                        else:
                            st.warning("⚠️ No exact match found in the GBIF Backbone.")
                    except Exception as e:
                        st.error(f"GBIF API Error: {e}")
            # ----------------------------------------------

            html_page = self.render_unified_page(selected_sp, meta_db=meta_db)
            st.components.v1.html(html_page, height=820, scrolling=True)
            # TAR panel
            ti = art.get("temporal_index", {})
            if not ti or not ti.get("latest_year"):
                ti = self.refresh_temporal_index(selected_sp, meta_db=meta_db)
            ep = ti.get("earliest_primary_year"); es = ti.get("earliest_secondary_year")
            ly = ti.get("latest_year")
            if ep or es or ly:
                st.markdown("##### 📅 Temporal Evidence Anchor")
                tc1,tc2,tc3 = st.columns(3)
                tc1.metric("Earliest Primary",   ep or "—")
                tc2.metric("Earliest Secondary", es or "—")
                tc3.metric("Latest Evidence",    ly or "—")
            # Map
            occ_pts = art.get("occurrence_points",[])
            map_pts = [{"lat": p.get("latitude"), "lon": p.get("longitude"),
                        "name": p.get("locality","?"), "type": p.get("occurrenceType","?")}
                       for p in occ_pts if p.get("latitude") is not None and p.get("longitude") is not None]
            if map_pts:
                st.markdown("#### 🗺️ Occurrence Map")
                try:
                    import folium, pandas as pd
                    from streamlit_folium import st_folium
                    mdf = pd.DataFrame(map_pts)
                    m = folium.Map(location=[mdf["lat"].mean(), mdf["lon"].mean()],
                                   zoom_start=5, tiles="CartoDB positron")
                    for pt in map_pts:
                        clr = "green" if str(pt["type"]).lower()=="primary" else "blue"
                        folium.CircleMarker(
                            location=[pt["lat"], pt["lon"]], radius=6,
                            color=clr, fill=True, fill_color=clr, fill_opacity=0.7,
                            tooltip=f"<b>{selected_sp}</b><br>{pt['name']}",
                        ).add_to(m)
                    st_folium(m, width=800, height=450, returned_objects=[])
                except ImportError:
                    import pandas as pd
                    st.map(pd.DataFrame(map_pts)[["lat","lon"]], zoom=4)

        with bib_tab:
            st.markdown("#### 📚 Bibliography (Scientific Citations)")
            bib = art.get("bibliography", {})
            idx_bib = art.get("citation_index", {})
            if not bib:
                st.info("No bibliography registered. Add references via `add_reference_to_species()`.")
            else:
                st.markdown(f"**{len(bib)} references · {len(idx_bib)} cited in text**")
                md_bib = self.render_bibliography_markdown(art)
                st.markdown(md_bib)
                col1, col2 = st.columns(2)
                with col1:
                    bibtex = self.to_bibtex(art)
                    st.download_button("⬇ Download BibTeX", bibtex,
                                       file_name=f"{self._slug(selected_sp)}.bib",
                                       mime="text/plain")
                with col2:
                    md_full = self.render_species_markdown(selected_sp)
                    st.download_button("⬇ Download Markdown Article", md_full,
                                       file_name=f"{self._slug(selected_sp)}.md",
                                       mime="text/markdown")

        with raw_tab:
            st.json(art, expanded=False)
            if call_llm_fn:
                st.markdown("#### 🤖 LLM Wiki Enhancement")
                enhance_text = st.text_area("Paste new PDF chunk:", height=180, key="wiki_enhance_text")
                enhance_cite = st.text_input("Citation:", key="wiki_enhance_cite")
                if st.button("✨ Enhance Article", key="wiki_enhance_btn"):
                    if enhance_text.strip():
                        with st.spinner("Wiki Architect enhancing…"):
                            try:
                                self._enhance_with_llm(selected_sp, enhance_text, enhance_cite, call_llm_fn)
                                st.success("Enhanced and versioned ✅"); st.rerun()
                            except Exception as exc: st.error(f"Enhancement failed: {exc}")
                    else: st.warning("Paste text first.")

        with ver_tab:
            versions = self.list_versions("species", selected_sp)
            if not versions:
                st.info("No previous versions yet.")
            else:
                import pandas as pd
                st.dataframe(pd.DataFrame(versions), use_container_width=True, hide_index=True)
                rollback_ver = st.number_input("Rollback to version:", min_value=1,
                                               max_value=max(v["version"] for v in versions),
                                               step=1, key="wiki_rollback_ver")
                if st.button("⏪ Rollback", key="wiki_rollback_btn"):
                    ok = self.rollback("species", selected_sp, rollback_ver)
                    if ok: st.success(f"Rolled back to v{rollback_ver} ✅"); st.rerun()
                    else: st.error("Rollback failed.")

        st.divider()
        # Locality checklist with admin hierarchy
        with st.expander("📍 Locality Species Checklist + Admin Hierarchy"):
            loc_list = self.list_localities()
            if loc_list:
                sel_loc = st.selectbox("Locality:", loc_list, key="wiki_loc_unified")
                if sel_loc:
                    loc_art = self._read("locality", self._slug(sel_loc)) or {}
                    admin   = loc_art.get("admin_hierarchy", {})
                    if admin and admin.get("state"):
                        st.markdown(
                            f"**State:** {admin.get('state','')}  |  "
                            f"**District:** {admin.get('district','')}  |  "
                            f"**Block:** {admin.get('block','')}  |  "
                            f"**Subdistrict:** {admin.get('subdistrict','')}  "
                            f"*(match score: {admin.get('match_score',0):.0f})*"
                        )
                    sps = loc_art.get("species_checklist", [])
                    st.write(f"**{len(sps)} species at {sel_loc}:**")
                    cols = st.columns(2)
                    for i, s in enumerate(sps):
                        cols[i%2].markdown(f"• *{_strip_auth(s)}*")
                    lat = loc_art.get("decimalLatitude"); lon = loc_art.get("decimalLongitude")
                    if lat and lon:
                        import pandas as pd
                        st.map(pd.DataFrame([{"lat": lat, "lon": lon}]), zoom=7)
            else:
                st.info("No locality articles yet.")

    def _count_versions(self) -> int:
        try:
            con = sqlite3.connect(self.db_path)
            n   = con.execute("SELECT COUNT(*) FROM wiki_versions").fetchone()[0]
            con.close(); return n
        except: return 0

    # ── Backward-compatibility shims ──────────────────────────────────────
    def _load_article(self, section, slug): return self._read(section, slug)
    def list_species_articles(self): return self.list_species()
    @property
    def _slug_fn(self): return self._slug


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — GEOCODING CASCADE  v6.0
# ═════════════════════════════════════════════════════════════════════════════

def _to_float(v) -> Optional[float]:
    if v is None: return None
    try:
        f = float(str(v).strip())
        return None if str(v).strip() in ("0","") else f
    except: return None

def _has_coords(occ: dict) -> bool:
    return (_to_float(occ.get("decimalLatitude")) is not None and
            _to_float(occ.get("decimalLongitude")) is not None)

def _resolve_occurrence_table(conn: sqlite3.Connection) -> str:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "occurrences_v4" in names: return "occurrences_v4"
    if "occurrences"    in names: return "occurrences"
    raise sqlite3.OperationalError("No occurrence table found.")


class GeocodingCascade:
    """
    Five-tool geocoding cascade — v6.0
    ────────────────────────────────────────────────────────────────────────────
    Tool 1 · DMS parse
    Tool 2 · locality_hierarchy.db  SQLite-backed village fuzzy search
             → centroid fetched lazily from GPKG only when needed
             → admin context (district/state) used for disambiguation
             → LocalityIdentifier builds the enriched query string
    Tool 3 · IndianPincodeGeocoder
    Tool 4 · GeoNames IN SQLite
    Tool 5 · Nominatim enriched fallback

    v6.0 changes vs v5.6:
      • hierarchy_db is the PRIMARY data source (replaces full geopandas load)
      • LocalityIdentifier.build_enriched_locality() improves fuzzy precision
      • Admin hierarchy stored on occ as  _admin_resolved  for wiki ingestion
      • _validate_with_gpkg is geometry-only (loaded lazily per-match)
    """

    def __init__(
        self,
        geonames_db:     str  = "",
        pincode_txt:     str  = "",
        pincode_state:   Optional[str] = None,
        use_nominatim:   bool = False,
        nominatim_agent: str  = "BioTrace_v6",
        gpkg_path:       str  = "biodiversity_data/destination_gpkg_folder/combined_layers.gpkg",
        hierarchy_db:    str  = "biodiversity_data/locality_hierarchy.db",
    ):
        self.geonames_db = geonames_db
        self.use_nominatim = use_nominatim
        self.gpkg_path = gpkg_path
        self.hierarchy_db = hierarchy_db

        # Shared LocalityIdentifier (same instance for enrichment & disambiguation)
        self._loc_id = LocalityIdentifier(hierarchy_db=hierarchy_db)

        # Tool 3 — Pincode
        self._pincode = None
        if pincode_txt and os.path.exists(pincode_txt):
            try:
                from pincode_geocoder import IndianPincodeGeocoder
                self._pincode = IndianPincodeGeocoder(
                    pincode_txt, fuzzy_threshold=80.0, state_filter=pincode_state)
                logger.info("[geocoding] PincodeGeocoder ready")
            except Exception as exc:
                logger.warning("[geocoding] PincodeGeocoder init: %s", exc)

        # Tool 5 — Nominatim
        self._nominatim = None
        if use_nominatim:
            try:
                from nominatim_geocoder import NominatimEnrichedGeocoder
                self._nominatim = NominatimEnrichedGeocoder(
                    geonames_db_path=geonames_db, user_agent=nominatim_agent)
                logger.info("[geocoding] NominatimGeocoder ready")
            except Exception as exc:
                logger.warning("[geocoding] Nominatim init: %s", exc)

    # ── Tool 1 · DMS ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_dms(occ: dict) -> dict:
        try:
            from coord_utils import parse_dms
        except ImportError: return occ
        for field in ("decimalLatitude","decimalLongitude"):
            val = occ.get(field)
            if isinstance(val, str) and val.strip():
                parsed = parse_dms(val.strip())
                if parsed is not None: occ[field] = parsed
        return occ

    # ── Tool 2 · hierarchy_db fuzzy search ───────────────────────────────

    def _hierarchy_fuzzy_search(self, locality_string: str, occ: dict = None) -> Optional[dict]:
        """
        v6.0 core:
          1. Build enriched locality string via LocalityIdentifier
          2. Exact → fuzzy match against village_cache in LocalityIdentifier
          3. Disambiguate using admin context
          4. Fetch centroid geometry lazily from GPKG

        Returns dict {decimalLatitude, decimalLongitude, geocodingSource,
                       polygon_matched, admin_resolved} or None.
        """
        if not locality_string:
            return None

        # Step 1 — enriched query (adds district/state if resolvable)
        enriched = self._loc_id.build_enriched_locality(locality_string, occ)

        # Step 2 — resolve admin (returns village row with state/district)
        admin = self._loc_id.resolve_to_admin(enriched) or self._loc_id.resolve_to_admin(locality_string)
        if not admin:
            return None

        match_score = admin.get("match_score", 0)
        layer_name  = admin.get("layer_name", "")
        v_name      = admin.get("village", locality_string)

        source_str = f"GPKG_HierarchyDB_{match_score:.0f}"
        if match_score < 85:
            source_str += "_LowConf"

        # Step 3 — lazy geometry fetch from GPKG
        if self.gpkg_path and os.path.exists(self.gpkg_path) and layer_name:
            try:
                import geopandas as gpd
                v_safe = v_name.replace("'", "''")
                # Try common village column names
                for v_col in ("village", "vill_name", "name", "gn_name"):
                    try:
                        sql = f"SELECT geometry FROM \"{layer_name}\" WHERE {v_col} = '{v_safe}' LIMIT 1"
                        matched_gdf = gpd.read_file(self.gpkg_path, engine="pyogrio", sql=sql)
                        if not matched_gdf.empty:
                            centroid = matched_gdf.iloc[0].geometry.centroid
                            return {
                                "decimalLatitude":  float(centroid.y),
                                "decimalLongitude": float(centroid.x),
                                "geocodingSource":  source_str,
                                "polygon_matched":  v_name,
                                "admin_resolved": {
                                    "state":       admin.get("state",""),
                                    "district":    admin.get("district",""),
                                    "block":       admin.get("block",""),
                                    "subdistrict": admin.get("subdistrict",""),
                                },
                            }
                    except Exception:
                        continue
            except Exception as exc:
                logger.warning("[geocoding] GPKG lazy load failed for %s: %s", v_name, exc)

        # Step 4 — no GPKG geometry but we have admin → return None so
        # downstream tools can try (Pincode/GeoNames may geocode the district)
        logger.debug("[geocoding] hierarchy match '%s' (score=%d) but no GPKG geometry",
                     v_name, match_score)
        return None

    # ── Coordinate validation ─────────────────────────────────────────────

    @staticmethod
    def _validate(occ: dict) -> dict:
        try:
            from coord_utils import validate_occurrence_coordinates
            return validate_occurrence_coordinates(occ)
        except Exception: return occ

    # ── Public: geocode_batch ─────────────────────────────────────────────

    def geocode_batch(self, occurrences: list) -> list:
        if not occurrences: return occurrences
        result = []

        for occ in occurrences:
            if not isinstance(occ, dict): result.append(occ); continue

            # Tool 1 — DMS
            occ = self._parse_dms(occ)

            if _has_coords(occ):
                occ.setdefault("geocodingSource","LLM")
                occ = self._validate(occ)
                result.append(occ); continue

            locality = str(occ.get("_geocodingLocality") or occ.get("verbatimLocality","")).strip()

            # Tool 2 — hierarchy_db fuzzy (v6.0)
            if locality:
                res = self._hierarchy_fuzzy_search(locality, occ)
                if res:
                    occ["decimalLatitude"]  = res["decimalLatitude"]
                    occ["decimalLongitude"] = res["decimalLongitude"]
                    occ["geocodingSource"]  = res["geocodingSource"]
                    occ["polygon_matched"]  = res.get("polygon_matched","")
                    occ["_admin_resolved"]  = res.get("admin_resolved", {})   # propagate to wiki
                    occ = self._validate(occ)
                    result.append(occ); continue

            # Tool 3 — Pincode
            if self._pincode and locality:
                try:
                    gr = self._pincode.geocode(locality)
                    if gr and gr.latitude is not None:
                        occ["decimalLatitude"]  = gr.latitude
                        occ["decimalLongitude"] = gr.longitude
                        occ["geocodingSource"]  = f"IN_Pincode_{gr.match_type}_{gr.score:.0f}"
                        occ = self._validate(occ)
                        result.append(occ); continue
                except Exception: pass

            # Tool 4 — GeoNames
            if locality and self.geonames_db and os.path.exists(self.geonames_db):
                try:
                    con = sqlite3.connect(self.geonames_db, check_same_thread=False)
                    res = con.execute(
                        "SELECT latitude,longitude FROM geonames "
                        "WHERE (name=? OR asciiname=? OR alternatenames LIKE ?) "
                        "AND country_code='IN' "
                        "ORDER BY CASE feature_class WHEN 'P' THEN 1 WHEN 'A' THEN 2 ELSE 3 END, "
                        "CAST(population AS INTEGER) DESC LIMIT 1",
                        (locality, locality, f"%{locality}%")
                    ).fetchone()
                    con.close()
                    if res:
                        occ["decimalLatitude"]  = float(res[0])
                        occ["decimalLongitude"] = float(res[1])
                        occ["geocodingSource"]  = "GeoNames_IN"
                        occ = self._validate(occ)
                        result.append(occ); continue
                except Exception: pass

            result.append(occ)

        # Tool 5 — Nominatim batch
        if self._nominatim:
            missing = [o for o in result if isinstance(o,dict) and not _has_coords(o)
                       and o.get("verbatimLocality")]
            if missing:
                logger.info("[geocoding] Nominatim: %d unresolved", len(missing))
                try:
                    geocoded = self._nominatim.geocode_missing(missing)
                    geocoded = [self._validate(o) for o in geocoded]
                    id_map   = {id(o): o for o in geocoded}
                    result   = [id_map.get(id(o), o) for o in result]
                except Exception as exc:
                    logger.warning("[geocoding] Nominatim batch: %s", exc)

        filled = sum(1 for o in result if isinstance(o,dict) and _has_coords(o))
        logger.info("[geocoding] %d/%d records geocoded", filled, len(result))
        return result

    def geocode_single(self, occ: dict) -> dict:
        return self.geocode_batch([occ])[0]

    def batch_geocode_db(self, meta_db_path: str, progress_callback=None) -> int:
        con   = sqlite3.connect(meta_db_path, check_same_thread=False)
        table = _resolve_occurrence_table(con)
        rows  = con.execute(
            f"SELECT id,verbatimLocality FROM {table} "
            "WHERE (decimalLatitude IS NULL OR decimalLongitude IS NULL) "
            "AND verbatimLocality IS NOT NULL AND verbatimLocality != '' "
            "AND validationStatus != 'rejected'"
        ).fetchall()
        if not rows: con.close(); return 0
        updated = 0
        for i, (row_id, vl) in enumerate(rows):
            occ = self.geocode_single({"verbatimLocality": vl,
                                       "decimalLatitude": None, "decimalLongitude": None})
            lat = _to_float(occ.get("decimalLatitude"))
            lon = _to_float(occ.get("decimalLongitude"))
            if lat is not None and lon is not None:
                con.execute(
                    f"UPDATE {table} SET decimalLatitude=?,decimalLongitude=?,geocodingSource=? WHERE id=?",
                    (lat, lon, occ.get("geocodingSource",""), row_id))
                updated += 1
            if updated % 50 == 0 and updated > 0: con.commit()
            if progress_callback: progress_callback(i + 1, len(rows))
        con.commit(); con.close()
        logger.info("[geocoding/db] %d/%d updated", updated, len(rows))
        return updated


# ═════════════════════════════════════════════════════════════════════════════
#  QUICK SMOKE TEST
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile

    print("=" * 70)
    print("BioTrace v6.0 — unified wiki + geocoding smoke test")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp:
        wiki = BioTraceWikiUnified(root_dir=tmp)

        # 1. Ingest an occurrence
        occ = {
            "validName":      "Marionia pambanensis",
            "verbatimLocality": "Pamban, Tamil Nadu",
            "decimalLatitude":  9.275, "decimalLongitude": 79.194,
            "occurrenceType":   "Primary",
            "phylum": "Mollusca", "class_": "Gastropoda",
            "order_": "Nudibranchia", "family_": "Polyceridae",
            "habitat": "intertidal rocky shore",
        }
        wiki.update_from_occurrences([occ], citation="Nanda et al. (2023)")

        # 2. Register a scientific reference
        ref = {
            "canonical_key": "doi:10.6024/jmbai.2023",
            "authors": ["Nanda, S.", "Hatkar, P.", "Vachhrajani, K."],
            "year": 2023,
            "title": "First distribution record of the Pamban sea slug",
            "journal_name": "Journal of the Marine Biological Association of India",
            "volume": "65", "issue": "2", "pages": "2427-18",
            "doi": "10.6024/jmbai.2023.65.2.2427-18",
            "source_type": "journal",
        }
        wiki.add_reference_to_species("Marionia pambanensis", ref)

        # 3. Read back and verify
        art = wiki.get_species_article("Marionia pambanensis")
        assert art is not None, "Article should exist"
        assert "doi:10.6024/jmbai.2023" in art.get("bibliography", {}), "Reference missing"
        print("  ✓ Species article created with bibliography")

        # 4. Markdown export
        md = wiki.render_species_markdown("Marionia pambanensis")
        assert "References" in md, "Bibliography section missing from markdown"
        print("  ✓ Markdown export with bibliography OK")

        # 5. BibTeX export
        bib = wiki.to_bibtex(art)
        assert "@article{doi:10.6024/jmbai.2023" in bib, "BibTeX entry missing"
        print("  ✓ BibTeX export OK")

        # 6. LocalityIdentifier (no real DB, just API test)
        loc_id = LocalityIdentifier(hierarchy_db="nonexistent.db")
        enriched = loc_id.build_enriched_locality("Narara", {"district": "Jamnagar"})
        assert "Narara" in enriched
        print("  ✓ LocalityIdentifier.build_enriched_locality OK")

        # 7. GeocodingCascade (no real DBs, just API test)
        geo = GeocodingCascade()
        result = geo.geocode_batch([
            {"verbatimLocality": "Pamban", "decimalLatitude": None, "decimalLongitude": None}
        ])
        assert isinstance(result, list)
        print("  ✓ GeocodingCascade.geocode_batch API OK")

        print("\n✓ All smoke tests passed")
        print("=" * 70)
