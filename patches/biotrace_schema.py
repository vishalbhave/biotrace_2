"""biotrace_schema.py — auto-generated stub by biotrace_patch57_update.py"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class OccurrenceRecord:
    validName: str = ''
    recordedName: str = ''
    verbatimLocality: str = ''
    decimalLatitude: Optional[float] = None
    decimalLongitude: Optional[float] = None
    occurrenceType: str = 'Uncertain'
    sourceCitation: str = ''
    kingdom: str = ''
    phylum: str = ''
    class_: str = ''
    order_: str = ''
    family_: str = ''
    genus: str = ''
    depthRange: str = ''
    habitat: str = ''
    eventDate: str = ''
    taxonomicStatus: str = ''
    wormsID: str = ''
    gbifID: str = ''
    iucnStatus: str = ''

@dataclass
class ExtractionResult:
    occurrences: List[OccurrenceRecord] = field(default_factory=list)
    species_names: List[str] = field(default_factory=list)
    citation: str = ''
    document_title: str = ''
