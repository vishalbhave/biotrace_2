"""
biotrace_author_extractor.py  —  BioTrace v5.3+ Author Blacklisting
────────────────────────────────────────────────────────────────────────────
AuthorExtractorAgent: Reads document header/abstract and isolates author names.
Passes them to LocalityNER as an explicit blacklist to prevent author names
from being misidentified as habitats or localities.

Problem:
  • Scientific PDFs often list authors in header/abstract:
    "Smith et al. (2020) found X at Location Y"
    "Authors: Dr. Sarah Johnson, Dr. Michael Lee, Dr. Jane Williams"
  
  • LocalityNER may confuse "Johnson" with "Johns Island" or "Smith" with
    a place name
  
  • This is especially problematic with common surnames that overlap with
    place names: "Wilson" (surname/place), "Grant" (surname/place),
    "Foster" (surname/place)

Solution:
  1. Scan document metadata + first ~2000 characters for author patterns:
     - "Authors: Name1, Name2, ..."
     - "First Author et al."
     - Structured author fields (if metadata available)
  
  2. Extract and normalize names:
     - "Dr. Smith" → "smith"
     - "Michael A. Johnson" → "michael", "johnson"
     - Remove prefixes (Dr., Prof., etc.)
  
  3. Pass to LocalityNER as author_blacklist parameter
  
  4. LocalityNER filters out matched names when extracting localities

Usage:
    from .author_extractor import AuthorExtractorAgent
    
    agent = AuthorExtractorAgent()
    
    # Basic usage
    authors = agent.extract_authors(document_header)
    # → {"smith", "johnson", "williams", ...}
    
    # With metadata
    authors = agent.extract_authors(
        text=document_header,
        metadata={"authors": ["Dr. J. Smith", "M. Johnson"]},
    )
    
    # Pass to LocalityNER
    from .locality_ner import LocalityNER
    lner = LocalityNER()
    localities = lner.extract_localities(
        text,
        author_blacklist=authors,
    )
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Set, Optional, Dict, List
from enum import Enum

logger = logging.getLogger("biotrace.author_extractor")

# ─────────────────────────────────────────────────────────────────────────────
#  PATTERNS FOR AUTHOR DETECTION
# ─────────────────────────────────────────────────────────────────────────────

# Common author prefixes to strip
_AUTHOR_PREFIXES = {
    "dr.", "dr", "prof.", "prof", "mr.", "mr", "ms.", "ms", "mrs.", "mrs",
    "sir", "lady", "gen.", "gen", "col.", "col", "fr.", "fr",
}

# Patterns for structured author sections
_AUTHOR_SECTION_PATTERNS = [
    r"(?:^|\n)\s*Author[s]?\s*[:\-]?\s*(.+?)(?:\n\n|\n[A-Z]|\Z)",
    r"(?:^|\n)\s*by\s+(.+?)(?:\n\n|\n[A-Z]|and|,|;|\Z)",
    r"(?:^|\n)\s*Written by\s+(.+?)(?:\n\n|\n[A-Z]|\Z)",
    r"(?:^|\n)\s*Submitted by\s+(.+?)(?:\n\n|\n[A-Z]|\Z)",
]

# Pattern for "et al." citations
_ET_AL_PATTERN = r"([A-Z][a-z]+)\s+et\s+al\.?(?:\s*\([0-9]{4}\))?\.?"

# Pattern for comma-separated name lists
_NAME_LIST_PATTERN = r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?),?\s*(?:and|&)\s+([A-Z][a-z]+)"

# Pattern for standalone author names (Capitalized FirstName LastName)
_NAME_PATTERN = r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?)\b"

# Common name prefixes and suffixes to handle
_NAME_PARTICLES = {"van", "von", "de", "la", "el", "bin", "abu", "jr", "sr", "ii", "iii"}

# Words that commonly appear near author sections but aren't names
_AUTHOR_CONTEXT_EXCLUDE = {
    "university", "institute", "department", "laboratory", "museum",
    "center", "college", "school", "academy", "foundation",
}


# ─────────────────────────────────────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuthorCandidate:
    """A potential author name identified in text."""
    full_name: str
    first_names: List[str]
    last_name: str
    prefixes: List[str]  # e.g., ["Dr.", "Prof."]
    confidence: float = 0.0
    source: str = "unknown"  # "et_al" | "structured" | "regex" | "metadata"
    
    def normalize(self) -> Set[str]:
        """
        Get normalized lowercase parts for blacklisting.
        
        Returns:
            set of lowercase name parts: {"smith", "john", "michael", ...}
        """
        parts = set()
        parts.add(self.last_name.lower())
        for fn in self.first_names:
            parts.add(fn.lower())
        return parts


# ─────────────────────────────────────────────────────────────────────────────
#  AUTHOR EXTRACTOR AGENT
# ─────────────────────────────────────────────────────────────────────────────

class AuthorExtractorAgent:
    """
    Extract author names from scientific document headers/metadata
    and create a blacklist to prevent name→locality confusion.
    """
    
    def __init__(self, max_authors: int = 50, min_confidence: float = 0.6):
        """
        Initialize the agent.
        
        Args:
            max_authors: Maximum authors to extract (prevent runaway)
            min_confidence: Minimum confidence threshold (0–1)
        """
        self.max_authors = max_authors
        self.min_confidence = min_confidence
        logger.info(
            f"[AuthorExtractor] init: max_authors={max_authors}, "
            f"min_confidence={min_confidence}"
        )
    
    def extract_authors(
        self,
        text: str,
        metadata: Optional[Dict] = None,
    ) -> Set[str]:
        """
        Extract author names from document text and metadata.
        
        Args:
            text: Document header or first N chars (~2000)
            metadata: Optional document metadata dict with 'authors' key
            
        Returns:
            set of lowercase name parts for blacklisting
        """
        candidates = []
        
        # Step 1: Extract from metadata if provided
        if metadata and 'authors' in metadata:
            candidates.extend(
                self._parse_metadata_authors(metadata['authors'])
            )
        
        # Step 2: Find structured author sections
        candidates.extend(self._find_structured_author_sections(text))
        
        # Step 3: Find "et al." citations
        candidates.extend(self._find_et_al_authors(text))
        
        # Step 4: Find name lists
        candidates.extend(self._find_name_lists(text))
        
        # Step 5: Find standalone names near top (but avoid false positives)
        candidates.extend(self._find_standalone_names(text))
        
        # Filter by confidence and deduplicate
        candidates = self._deduplicate_candidates(candidates)
        candidates = [c for c in candidates if c.confidence >= self.min_confidence]
        candidates = candidates[:self.max_authors]
        
        logger.debug(f"[AuthorExtractor] found {len(candidates)} author candidates")
        
        # Normalize to blacklist set
        blacklist = set()
        for candidate in candidates:
            blacklist.update(candidate.normalize())
        
        logger.info(f"[AuthorExtractor] blacklist has {len(blacklist)} terms")
        return blacklist
    
    # ─────────────────────────────────────────────────────────────────────────
    #  EXTRACTION STRATEGIES
    # ─────────────────────────────────────────────────────────────────────────
    
    def _parse_metadata_authors(
        self,
        author_list: List[str],
    ) -> List[AuthorCandidate]:
        """Extract authors from document metadata."""
        candidates = []
        for author_str in author_list[:self.max_authors]:
            candidate = self._parse_name_string(author_str, source="metadata")
            if candidate:
                candidate.confidence = 0.95  # High confidence from metadata
                candidates.append(candidate)
        return candidates
    
    def _find_structured_author_sections(self, text: str) -> List[AuthorCandidate]:
        """Find author names in structured "Authors:" sections."""
        candidates = []
        
        for pattern in _AUTHOR_SECTION_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL):
                author_block = match.group(1).strip()
                
                # Split on common separators
                author_strings = re.split(r',|;|and|&', author_block)
                
                for author_str in author_strings:
                    author_str = author_str.strip()
                    if author_str and len(author_str) > 2:  # Not too short
                        candidate = self._parse_name_string(
                            author_str, source="structured"
                        )
                        if candidate:
                            candidate.confidence = 0.85
                            candidates.append(candidate)
        
        return candidates
    
    def _find_et_al_authors(self, text: str) -> List[AuthorCandidate]:
        """Extract first author from 'X et al.' citations."""
        candidates = []
        
        for match in re.finditer(_ET_AL_PATTERN, text):
            name_part = match.group(1)
            candidate = AuthorCandidate(
                full_name=name_part,
                first_names=[],
                last_name=name_part,
                prefixes=[],
                confidence=0.75,
                source="et_al",
            )
            candidates.append(candidate)
        
        return candidates
    
    def _find_name_lists(self, text: str) -> List[AuthorCandidate]:
        """Extract names from 'Name1 and Name2' patterns."""
        candidates = []
        
        for match in re.finditer(_NAME_LIST_PATTERN, text):
            for name in [match.group(1), match.group(2)]:
                candidate = self._parse_name_string(name, source="name_list")
                if candidate:
                    candidate.confidence = 0.70
                    candidates.append(candidate)
        
        return candidates
    
    def _find_standalone_names(
        self,
        text: str,
        max_chars: int = 1000,
    ) -> List[AuthorCandidate]:
        """
        Extract standalone names from document beginning.
        Careful not to pick up arbitrary Capitalized words.
        """
        candidates = []
        
        # Only scan first portion
        text_head = text[:max_chars]
        
        # Look for name patterns but avoid context that suggests they're not authors
        for match in re.finditer(_NAME_PATTERN, text_head):
            full_match = match.group(0)
            
            # Skip if preceded by common non-author context
            start_pos = match.start()
            context_before = text_head[max(0, start_pos - 50):start_pos].lower()
            
            # Check if this looks like author context (near "by", "author", etc.)
            is_author_context = bool(
                re.search(r'\b(by|author|written|submitted|from)\b', context_before)
            )
            
            # Also accept if names appear early and in list-like context
            is_list_context = full_match[0] == match.group(0)[0] and (
                ',' in text_head[start_pos:start_pos+50] or
                'and' in text_head[start_pos:start_pos+50]
            )
            
            if is_author_context or is_list_context:
                candidate = self._parse_name_string(
                    full_match, source="standalone"
                )
                if candidate:
                    candidate.confidence = 0.65
                    candidates.append(candidate)
        
        return candidates
    
    # ─────────────────────────────────────────────────────────────────────────
    #  NAME PARSING
    # ─────────────────────────────────────────────────────────────────────────
    
    def _parse_name_string(
        self,
        name_str: str,
        source: str = "unknown",
    ) -> Optional[AuthorCandidate]:
        """
        Parse a name string into structured parts.
        
        Handles: "Dr. John Michael Smith", "van der Berg", "Smith, Jr."
        
        Args:
            name_str: Author name string
            source: Where it came from (for confidence scoring)
            
        Returns:
            AuthorCandidate or None if parsing fails
        """
        name_str = name_str.strip()
        if not name_str or len(name_str) < 2:
            return None
        
        # Extract prefixes
        prefixes = []
        remaining = name_str
        
        # Check for common title prefixes
        for prefix in _AUTHOR_PREFIXES:
            if remaining.lower().startswith(prefix):
                prefixes.append(prefix)
                remaining = remaining[len(prefix):].strip()
                break
        
        # Remove trailing degrees/qualifiers
        remaining = re.sub(r'\s*,\s*(jr|sr|ii|iii|phd|md|dvm)\.?$', '', remaining, flags=re.IGNORECASE)
        
        # Parse remaining into names
        parts = remaining.split()
        if not parts:
            return None
        
        # Detect particles (von, van, de, etc.)
        name_particles = []
        first_names = []
        last_name = ""
        
        i = 0
        while i < len(parts):
            part = parts[i]
            part_lower = part.lower().rstrip('.,')
            
            if part_lower in _NAME_PARTICLES and i < len(parts) - 1:
                # This is a particle; include it with next part
                name_particles.append(part)
                i += 1
            elif len(part) > 1:  # Not a single initial
                break
            else:
                first_names.append(part.rstrip('.'))
                i += 1
        
        # Remaining parts are likely last name
        if i < len(parts):
            last_parts = parts[i:]
            last_name = ' '.join(name_particles + last_parts).rstrip('.,')
        
        if not last_name:
            # No clear structure; assume last part is surname
            if parts:
                last_name = parts[-1].rstrip('.,')
                first_names = parts[:-1]
        
        if not last_name:
            return None
        
        return AuthorCandidate(
            full_name=name_str,
            first_names=first_names,
            last_name=last_name,
            prefixes=prefixes,
            confidence=0.0,  # Will be set by caller
            source=source,
        )
    
    def _deduplicate_candidates(
        self,
        candidates: List[AuthorCandidate],
    ) -> List[AuthorCandidate]:
        """
        Remove duplicate candidates (same person, different representations).
        Keep higher-confidence versions.
        """
        seen = {}
        deduplicated = []
        
        # Sort by confidence descending
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        
        for candidate in candidates:
            # Normalize key: lowercase last name + first initial
            key = candidate.last_name.lower()
            if candidate.first_names:
                key += f"_{candidate.first_names[0][0].lower()}"
            
            if key not in seen:
                seen[key] = candidate
                deduplicated.append(candidate)
        
        return deduplicated


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def extract_author_blacklist(
    text: str,
    metadata: Optional[Dict] = None,
) -> Set[str]:
    """
    Convenience function: extract author blacklist in one call.
    
    Args:
        text: Document header/first ~2000 chars
        metadata: Optional document metadata
        
    Returns:
        set of lowercase name parts for blacklisting
    """
    agent = AuthorExtractorAgent()
    return agent.extract_authors(text, metadata=metadata)


def test_author_extraction():
    """Quick test of author extraction."""
    test_cases = [
        "Authors: John Smith and Michael Johnson",
        "By Dr. Sarah Williams, Dr. Robert Johnson",
        "Smith et al. (2020) found...",
        "Submitted by: Jane Doe, Michael Chen & Robert Foster",
    ]
    
    agent = AuthorExtractorAgent()
    
    for text in test_cases:
        blacklist = agent.extract_authors(text)
        print(f"Text: {text[:50]}...")
        print(f"  → Blacklist: {sorted(blacklist)}\n")


if __name__ == "__main__":
    test_author_extraction()
