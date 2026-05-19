"""
biotrace_gna_inverted.py  —  BioTrace v5.3+ GNA-First Pipeline
────────────────────────────────────────────────────────────────────────────
Inverted Extraction (GNA-First): The pipeline now runs TaxonNER (GNA APIs) 
over the de-matrixed text FIRST. For every verified species found, it extracts 
a tight ±400 character window around that exact mention and asks the LLM to 
extract the locality. Context clash is completely eliminated.

Key innovation: WINDOWING
  • For each verified species mention in the text, extract a local context window
    (±400 chars centered on the species mention)
  • Pass ONLY this window + the species name to the LocalityNER
  • This eliminates cross-contamination where "locality_A" at page 5 corrupts
    extraction for "species_B" at page 15

Architecture:
  ┌─ Step 1: Table De-matrixing ──────────────────────────────────────────┐
  │  TablePreprocessorAgent detects and rewrites mangled tables to prose   │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Step 2: Species Discovery (GNA-First) ────────────────────────────────┐
  │  TaxonNER.extract() → GNA Finder + Verifier + Disambiguation           │
  │  Output: list[TaxonCandidate] with char offsets                        │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Step 3: Context Windowing ────────────────────────────────────────────┐
  │  For each TaxonCandidate with gna_valid=True:                          │
  │    • Extract ±400 char context window                                  │
  │    • Store {species, window_text, char_offset}                         │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Step 4: Author Blacklisting ──────────────────────────────────────────┐
  │  AuthorExtractorAgent reads first ~2000 chars, isolates author names   │
  │  Passes them to LocalityNER as explicit blacklist                      │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Step 5: Locality Extraction (per-window) ─────────────────────────────┐
  │  For each windowed context:                                            │
  │    LocalityNER.extract_localities(window, blacklist=authors)           │
  │    → Avoids "Smith" (author) becoming a habitat                        │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Step 6: Occurrence Assembly ──────────────────────────────────────────┐
  │  Link each species → nearest locality(ies) found in its window         │
  │  Deduplicate + rank by confidence                                      │
  └───────────────────────────────────────────────────────────────────────┘

Usage:
    from biotrace_gna_inverted import GNAFirstPipeline
    
    pipeline = GNAFirstPipeline(
        geonames_db="path/to/geonames_india.db",
        author_blacklist_cache=True
    )
    
    # Orchestrate the full pipeline
    occurrences = pipeline.process_document(
        text,
        source_file="Chapter_3.pdf"
    )
    
    # Or step-by-step:
    cleaned_text = pipeline.preprocess_tables(text)
    species = pipeline.extract_gna_verified_species(cleaned_text)
    authors = pipeline.extract_authors(text[:2000])
    windowed_contexts = pipeline.build_windowed_contexts(
        text, species, window_size=400
    )
    localities = pipeline.extract_localities_per_window(
        windowed_contexts, author_blacklist=authors
    )
    occurrences = pipeline.link_species_to_localities(
        species, localities, windowed_contexts
    )
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Set
from datetime import datetime

logger = logging.getLogger("biotrace.gna_inverted")

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
_WINDOW_SIZE = 400  # chars either side of species mention
_AUTHOR_FIRST_N_CHARS = 2000  # scan this much of document for authors
_TIMEOUT = 15
_MIN_GNA_SCORE = 0.80


# ─────────────────────────────────────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SpeciesWindow:
    """A species mention with its extracted context window."""
    species_name: str
    canonical_name: str
    verbatim: str
    char_start: int
    char_end: int
    gna_valid: bool
    valid_name: str
    data_source: str
    match_score: float
    
    # Window info
    window_start: int
    window_end: int
    window_text: str
    
    # GNA metadata
    worms_id: Optional[str] = None
    itis_id: Optional[str] = None
    phylum: Optional[str] = None
    class_: Optional[str] = None
    order_: Optional[str] = None
    family_: Optional[str] = None
    taxonomic_status: str = "unverified"


@dataclass
class LocalityInWindow:
    """A locality found within a specific species' context window."""
    raw: str
    expanded: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    admin1: Optional[str] = None  # state/province
    admin2: Optional[str] = None  # district
    admin3: Optional[str] = None  # taluka/tehsil
    feature_type: str = "unknown"
    confidence: float = 0.0
    source: str = "geonames"  # "geonames" | "nominatim" | "regex" | "llm"
    from_window: bool = True


@dataclass
class LinkedOccurrence:
    """Final occurrence: species + associated localities."""
    species_name: str
    canonical_name: str
    localities: List[LocalityInWindow] = field(default_factory=list)
    primary_locality: Optional[LocalityInWindow] = None
    gna_valid: bool = False
    match_score: float = 0.0
    data_source: str = "unknown"
    occurrence_type: str = "Primary"
    extracted_from_window: bool = True
    window_start: int = 0
    window_end: int = 0


# ─────────────────────────────────────────────────────────────────────────────
#  GNA-FIRST PIPELINE CLASS
# ─────────────────────────────────────────────────────────────────────────────

class GNAFirstPipeline:
    """
    Orchestrator for GNA-first inverted extraction with windowing,
    table de-matrixing, and author blacklisting.
    """
    
    def __init__(
        self,
        geonames_db: Optional[str] = None,
        taxon_ner = None,
        locality_ner = None,
        author_blacklist_cache: bool = True,
    ):
        """
        Initialize the pipeline.
        
        Args:
            geonames_db: Path to GeoNames India SQLite database
            taxon_ner: TaxonNER instance (if None, lazy-loaded)
            locality_ner: LocalityNER instance (if None, lazy-loaded)
            author_blacklist_cache: Cache author lists per source_file
        """
        self.geonames_db = geonames_db
        self._taxon_ner = taxon_ner
        self._locality_ner = locality_ner
        self.author_blacklist_cache = {}
        self._cache_enabled = author_blacklist_cache
        
        logger.info(
            "[GNAFirstPipeline] initialized; "
            f"window_size={_WINDOW_SIZE}, "
            f"author_scan_chars={_AUTHOR_FIRST_N_CHARS}"
        )
    
    @property
    def taxon_ner(self):
        """Lazy-load TaxonNER on first use."""
        if self._taxon_ner is None:
            try:
                from biotrace_ner import TaxonNER
                self._taxon_ner = TaxonNER()
                logger.info("[GNAFirstPipeline] TaxonNER loaded")
            except ImportError as e:
                logger.error(f"Failed to import TaxonNER: {e}")
                raise
        return self._taxon_ner
    
    @property
    def locality_ner(self):
        """Lazy-load LocalityNER on first use."""
        if self._locality_ner is None:
            try:
                from biotrace_locality_ner import LocalityNER
                self._locality_ner = LocalityNER(geonames_db=self.geonames_db)
                logger.info("[GNAFirstPipeline] LocalityNER loaded")
            except ImportError as e:
                logger.error(f"Failed to import LocalityNER: {e}")
                raise
        return self._locality_ner
    
    # ─────────────────────────────────────────────────────────────────────────
    #  PUBLIC ORCHESTRATION
    # ─────────────────────────────────────────────────────────────────────────
    
    def process_document(
        self,
        text: str,
        source_file: str = "unknown",
        preprocess_tables: bool = True,
        cache_authors: bool = True,
    ) -> List[LinkedOccurrence]:
        """
        Full pipeline: clean → species → authors → windows → localities → occurrences.
        
        Args:
            text: Full document text
            source_file: Document name (for logging and caching)
            preprocess_tables: Run table de-matrixing
            cache_authors: Cache authors for this source_file
            
        Returns:
            list[LinkedOccurrence] — species linked with extracted localities
        """
        logger.info(f"[GNAFirstPipeline] processing {source_file}")
        start_time = time.time()
        
        # Step 1: De-matrixing (if enabled)
        if preprocess_tables:
            text = self.preprocess_tables(text)
            logger.debug(f"[GNAFirstPipeline] tables preprocessed")
        
        # Step 2: Extract author names from beginning
        authors = self.extract_authors(text[:_AUTHOR_FIRST_N_CHARS])
        if cache_authors and self._cache_enabled:
            self.author_blacklist_cache[source_file] = authors
        logger.info(f"[GNAFirstPipeline] found {len(authors)} author names")
        
        # Step 3: GNA-first species extraction
        species_windows = self.extract_gna_verified_species_windowed(text)
        logger.info(
            f"[GNAFirstPipeline] found {len(species_windows)} verified species"
        )
        
        # Step 4: Extract localities per window (with author blacklist)
        occurrences = []
        for sp_win in species_windows:
            # Extract localities from this species' context window
            localities = self._extract_localities_in_window(
                sp_win, authors
            )
            
            # Assemble occurrence
            occ = LinkedOccurrence(
                species_name=sp_win.species_name,
                canonical_name=sp_win.canonical_name,
                localities=localities,
                primary_locality=localities[0] if localities else None,
                gna_valid=sp_win.gna_valid,
                match_score=sp_win.match_score,
                data_source=sp_win.data_source,
                window_start=sp_win.window_start,
                window_end=sp_win.window_end,
            )
            occurrences.append(occ)
        
        elapsed = time.time() - start_time
        logger.info(
            f"[GNAFirstPipeline] {source_file} "
            f"→ {len(occurrences)} occurrences in {elapsed:.2f}s"
        )
        return occurrences
    
    # ─────────────────────────────────────────────────────────────────────────
    #  STEP 1: TABLE DE-MATRIXING
    # ─────────────────────────────────────────────────────────────────────────
    
    def preprocess_tables(self, text: str) -> str:
        """
        Detect mangled CSV/Markdown tables and rewrite them as prose.
        Delegates to TablePreprocessorAgent.
        
        Args:
            text: Full document text
            
        Returns:
            text with tables rewritten to prose
        """
        try:
            from biotrace_table_preprocessor import TablePreprocessorAgent
            agent = TablePreprocessorAgent()
            text = agent.process_document(text)
            return text
        except ImportError:
            logger.warning(
                "[GNAFirstPipeline] TablePreprocessorAgent not available; "
                "skipping table de-matrixing"
            )
            return text
    
    # ─────────────────────────────────────────────────────────────────────────
    #  STEP 2: AUTHOR EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────
    
    def extract_authors(self, text_head: str) -> Set[str]:
        """
        Extract author names from document header/abstract.
        Delegates to AuthorExtractorAgent.
        
        Args:
            text_head: First ~2000 chars of document
            
        Returns:
            set of lowercase author names for blacklisting
        """
        try:
            from biotrace_author_extractor import AuthorExtractorAgent
            agent = AuthorExtractorAgent()
            authors = agent.extract_authors(text_head)
            return authors
        except ImportError:
            logger.warning(
                "[GNAFirstPipeline] AuthorExtractorAgent not available; "
                "skipping author blacklisting"
            )
            return set()
    
    # ─────────────────────────────────────────────────────────────────────────
    #  STEP 3: GNA-FIRST SPECIES EXTRACTION WITH WINDOWING
    # ─────────────────────────────────────────────────────────────────────────
    
    def extract_gna_verified_species_windowed(
        self,
        text: str,
        window_size: int = _WINDOW_SIZE,
    ) -> List[SpeciesWindow]:
        """
        Run TaxonNER over full text, then extract context windows around 
        each verified species mention.
        
        Args:
            text: Full document text
            window_size: Context chars on either side (default 400)
            
        Returns:
            list[SpeciesWindow] — species with extracted context windows
        """
        # Step 1: Run TaxonNER to get all candidates
        candidates = self.taxon_ner.extract(text)
        logger.debug(f"TaxonNER found {len(candidates)} candidates")
        
        # Step 2: Filter for GNA-verified species
        verified = self.taxon_ner.verify_all(candidates)
        verified = [c for c in verified if c.gna_valid]
        logger.info(f"GNA verified {len(verified)} candidates")
        
        # Step 3: For each verified species, extract windowed context
        windows = []
        for taxon in verified:
            sw = self._build_species_window(text, taxon, window_size)
            windows.append(sw)
        
        return windows
    
    def _build_species_window(
        self,
        text: str,
        taxon_candidate,  # TaxonCandidate from biotrace_ner
        window_size: int,
    ) -> SpeciesWindow:
        """
        Extract a context window around a species mention.
        
        Args:
            text: Full document text
            taxon_candidate: TaxonCandidate with char offsets
            window_size: Chars on each side
            
        Returns:
            SpeciesWindow with extracted window_text
        """
        char_start = max(0, taxon_candidate.char_start - window_size)
        char_end = min(len(text), taxon_candidate.char_end + window_size)
        window_text = text[char_start:char_end]
        
        return SpeciesWindow(
            species_name=taxon_candidate.verbatim,
            canonical_name=taxon_candidate.valid_name or taxon_candidate.canonical,
            verbatim=taxon_candidate.verbatim,
            char_start=taxon_candidate.char_start,
            char_end=taxon_candidate.char_end,
            gna_valid=taxon_candidate.gna_valid,
            valid_name=taxon_candidate.valid_name or "",
            data_source=taxon_candidate.data_source or "unknown",
            match_score=taxon_candidate.match_score,
            window_start=char_start,
            window_end=char_end,
            window_text=window_text,
            worms_id=getattr(taxon_candidate, 'worms_id', None),
            itis_id=getattr(taxon_candidate, 'itis_id', None),
            phylum=getattr(taxon_candidate, 'phylum', None),
            class_=getattr(taxon_candidate, 'class_', None),
            order_=getattr(taxon_candidate, 'order_', None),
            family_=getattr(taxon_candidate, 'family_', None),
            taxonomic_status=getattr(taxon_candidate, 'taxonomic_status', 'unverified'),
        )
    
    # ─────────────────────────────────────────────────────────────────────────
    #  STEP 5: LOCALITY EXTRACTION PER WINDOW
    # ─────────────────────────────────────────────────────────────────────────
    
    def _extract_localities_in_window(
        self,
        sp_window: SpeciesWindow,
        author_blacklist: Set[str],
    ) -> List[LocalityInWindow]:
        """
        Extract localities from a species' context window,
        avoiding author names via blacklist.
        
        Args:
            sp_window: SpeciesWindow with window_text
            author_blacklist: Set of lowercase author names to exclude
            
        Returns:
            list[LocalityInWindow] sorted by confidence
        """
        # Extract localities from window using LocalityNER
        raw_localities = self.locality_ner.extract_localities(
            sp_window.window_text,
            author_blacklist=author_blacklist,
        )
        
        # Convert to LocalityInWindow objects
        localities = []
        for loc in raw_localities:
            loc_win = LocalityInWindow(
                raw=loc.get('raw', ''),
                expanded=loc.get('expanded', ''),
                latitude=loc.get('latitude'),
                longitude=loc.get('longitude'),
                admin1=loc.get('admin1'),
                admin2=loc.get('admin2'),
                admin3=loc.get('admin3'),
                feature_type=loc.get('feature_type', 'unknown'),
                confidence=loc.get('confidence', 0.0),
                source=loc.get('source', 'geonames'),
                from_window=True,
            )
            localities.append(loc_win)
        
        # Sort by confidence descending
        localities.sort(key=lambda x: x.confidence, reverse=True)
        return localities
    
    # ─────────────────────────────────────────────────────────────────────────
    #  STEP-BY-STEP INTERFACE (for advanced usage)
    # ─────────────────────────────────────────────────────────────────────────
    
    def build_windowed_contexts(
        self,
        text: str,
        species: List[SpeciesWindow],
        window_size: int = _WINDOW_SIZE,
    ) -> Dict[str, SpeciesWindow]:
        """
        Build windowed contexts keyed by species name.
        
        Returns:
            dict[species_name] → SpeciesWindow
        """
        return {sp.species_name: sp for sp in species}
    
    def extract_localities_per_window(
        self,
        windowed_contexts: Dict[str, SpeciesWindow],
        author_blacklist: Set[str],
    ) -> Dict[str, List[LocalityInWindow]]:
        """
        Extract localities for each windowed context.
        
        Returns:
            dict[species_name] → list[LocalityInWindow]
        """
        result = {}
        for species_name, sp_win in windowed_contexts.items():
            locs = self._extract_localities_in_window(sp_win, author_blacklist)
            result[species_name] = locs
        return result
    
    def link_species_to_localities(
        self,
        species: List[SpeciesWindow],
        localities: Dict[str, List[LocalityInWindow]],
        windowed_contexts: Dict[str, SpeciesWindow],
    ) -> List[LinkedOccurrence]:
        """
        Final step: assemble LinkedOccurrence objects linking species to localities.
        
        Returns:
            list[LinkedOccurrence]
        """
        occurrences = []
        for sp in species:
            locs = localities.get(sp.species_name, [])
            occ = LinkedOccurrence(
                species_name=sp.species_name,
                canonical_name=sp.canonical_name,
                localities=locs,
                primary_locality=locs[0] if locs else None,
                gna_valid=sp.gna_valid,
                match_score=sp.match_score,
                data_source=sp.data_source,
                window_start=sp.window_start,
                window_end=sp.window_end,
            )
            occurrences.append(occ)
        return occurrences


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def render_occurrence_table(occurrences: List[LinkedOccurrence]) -> str:
    """
    Format occurrences as a markdown table for display.
    
    Args:
        occurrences: List of LinkedOccurrence objects
        
    Returns:
        Markdown table string
    """
    lines = [
        "| Species | Canonical | Primary Locality | GNA Valid | Match Score |",
        "|---------|-----------|------------------|-----------|-------------|",
    ]
    
    for occ in occurrences:
        primary_loc = (
            f"{occ.primary_locality.expanded}"
            if occ.primary_locality
            else "—"
        )
        score_str = f"{occ.match_score:.3f}" if occ.match_score else "—"
        valid_str = "✓" if occ.gna_valid else "✗"
        
        lines.append(
            f"| {occ.species_name} | {occ.canonical_name} | {primary_loc} | "
            f"{valid_str} | {score_str} |"
        )
    
    return "\n".join(lines)
