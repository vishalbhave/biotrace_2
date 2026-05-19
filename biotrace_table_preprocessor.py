"""
biotrace_table_preprocessor.py  —  BioTrace v5.3+ Table De-matrixing
────────────────────────────────────────────────────────────────────────────
TablePreprocessorAgent: Detects mangled CSV/Markdown tables in scientific PDFs
and rewrites them as explicit prose sentences.

Problem (common in PDF-extracted text):
  • Tables often appear as:
    - "Species A | Location X | Date Y" (pipe-delimited but no header)
    - "Species\tLocation\tDate" (tab-delimited fragments)
    - Malformed Markdown tables (missing pipes, inconsistent columns)
    - Mixed line breaks destroying table structure
  
  • When passed to LLM/LocalityNER as-is, they create ambiguity:
    - "Location X" gets confused with text around it
    - Context windows around species get polluted with unrelated table data

Solution:
  1. Detect tables via regex + heuristics:
     - Rows with 3+ pipe/tab-separated fields
     - Consistent column count across rows
     - Header row (capitalized first field)
  
  2. Infer table structure:
     - What's the header? (first row, or detected from context)
     - How many columns? (field count)
     - What's each column semantics? (via regex: species, location, date)
  
  3. Rewrite as prose:
     - "Species A was found at Location X in Year Y."
     - Preserves all information without structural ambiguity

Usage:
    from biotrace_table_preprocessor import TablePreprocessorAgent
    
    agent = TablePreprocessorAgent()
    clean_text = agent.process_document(extracted_pdf_text)
    
    # Advanced:
    tables = agent.detect_tables(text)
    for tbl in tables:
        print(tbl.to_markdown())  # what it detected
        prose = agent.table_to_prose(tbl)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from enum import Enum

logger = logging.getLogger("biotrace.table_preprocessor")

# ─────────────────────────────────────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

class ColumnType(Enum):
    """Semantic type of a table column."""
    SPECIES = "species"
    LOCATION = "location"
    DATE = "date"
    HABITAT = "habitat"
    NOTES = "notes"
    UNKNOWN = "unknown"


@dataclass
class TableCell:
    """Single cell in a detected table."""
    content: str
    row: int
    col: int
    column_type: ColumnType = ColumnType.UNKNOWN


@dataclass
class DetectedTable:
    """A table detected in document text."""
    start_char: int
    end_char: int
    raw_text: str
    rows: List[List[str]]
    column_count: int
    column_types: List[ColumnType] = field(default_factory=list)
    has_header: bool = False
    header: List[str] = field(default_factory=list)
    delimiter: str = "|"  # pipe, tab, or inferred
    confidence: float = 0.0
    
    def to_markdown(self) -> str:
        """Render detected table as Markdown."""
        if not self.rows:
            return ""
        
        lines = []
        
        # Header
        if self.header:
            lines.append("| " + " | ".join(self.header) + " |")
            lines.append("|" + "|".join(["---"] * len(self.header)) + "|")
        
        # Rows
        for row in self.rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        
        return "\n".join(lines)
    
    def infer_column_types(self):
        """Auto-detect column types (species, location, date, etc.)."""
        if not self.rows or not self.rows[0]:
            return
        
        col_count = len(self.rows[0])
        self.column_types = [ColumnType.UNKNOWN] * col_count
        
        # Sample content from each column
        for col_idx in range(col_count):
            col_content = " ".join(
                str(row[col_idx] if col_idx < len(row) else "")
                for row in self.rows[:5]
            ).lower()
            
            # Heuristic detection
            if re.search(r'\b(species|taxon|genus|binomial|organism)\b', col_content):
                self.column_types[col_idx] = ColumnType.SPECIES
            elif re.search(r'\b(location|locality|site|station|region|country|state|district)\b', col_content):
                self.column_types[col_idx] = ColumnType.LOCATION
            elif re.search(r'\b(date|year|month|day|when)\b', col_content):
                self.column_types[col_idx] = ColumnType.DATE
            elif re.search(r'\b(habitat|environment|ecosystem|beach|coral|reef)\b', col_content):
                self.column_types[col_idx] = ColumnType.HABITAT
            elif re.search(r'\b(notes?|remarks?|observations?|comment)\b', col_content):
                self.column_types[col_idx] = ColumnType.NOTES


# ─────────────────────────────────────────────────────────────────────────────
#  TABLE PREPROCESSOR AGENT
# ─────────────────────────────────────────────────────────────────────────────

class TablePreprocessorAgent:
    """
    Detect and de-matrix tables in scientific PDFs.
    Rewrites them as prose to avoid locality/species cross-contamination.
    """
    
    def __init__(self, min_columns: int = 3, min_rows: int = 2):
        """
        Initialize the agent.
        
        Args:
            min_columns: Minimum column count to consider a table
            min_rows: Minimum row count to consider a table
        """
        self.min_columns = min_columns
        self.min_rows = min_rows
        
        # Regex patterns for table detection
        self._pipe_delim_re = re.compile(r'^(.+\|){' + str(min_columns - 1) + r',}.+$', re.MULTILINE)
        self._tab_delim_re = re.compile(r'^(.+\t){' + str(min_columns - 1) + r',}.+$', re.MULTILINE)
        
        logger.info(
            f"[TablePreprocessor] init: min_columns={min_columns}, "
            f"min_rows={min_rows}"
        )
    
    def process_document(self, text: str) -> str:
        """
        Process full document text: detect and rewrite all tables.
        
        Args:
            text: Full document text (from Docling extraction)
            
        Returns:
            text with tables replaced by prose equivalents
        """
        tables = self.detect_tables(text)
        logger.info(f"[TablePreprocessor] detected {len(tables)} tables")
        
        if not tables:
            return text
        
        # Replace tables from end to start (preserve character offsets)
        result = text
        for table in reversed(tables):
            prose = self.table_to_prose(table)
            if prose:
                result = (
                    result[:table.start_char]
                    + prose
                    + result[table.end_char:]
                )
        
        return result
    
    def detect_tables(self, text: str) -> List[DetectedTable]:
        """
        Scan document for tables using regex + heuristics.
        
        Returns:
            list[DetectedTable] sorted by start position
        """
        tables = []
        
        # Strategy 1: Pipe-delimited rows
        tables.extend(self._detect_pipe_delimited_tables(text))
        
        # Strategy 2: Tab-delimited rows
        tables.extend(self._detect_tab_delimited_tables(text))
        
        # Strategy 3: Markdown table format
        tables.extend(self._detect_markdown_tables(text))
        
        # Sort by start position and deduplicate overlapping tables
        tables.sort(key=lambda t: t.start_char)
        tables = self._deduplicate_overlapping(tables)
        
        return tables
    
    def _detect_pipe_delimited_tables(self, text: str) -> List[DetectedTable]:
        """Detect tables with pipe delimiters."""
        tables = []
        
        # Find consecutive lines with pipe delimiters
        lines = text.split('\n')
        i = 0
        while i < len(lines):
            # Check if this line has multiple pipes
            if lines[i].count('|') >= self.min_columns - 1:
                # Scan forward for consecutive pipe-delimited rows
                table_lines = [lines[i]]
                j = i + 1
                while j < len(lines) and lines[j].count('|') >= self.min_columns - 1:
                    table_lines.append(lines[j])
                    j += 1
                
                # Only if we have enough rows
                if len(table_lines) >= self.min_rows:
                    table = self._parse_delimited_table(
                        table_lines, delimiter='|', start_offset=sum(len(l) + 1 for l in lines[:i])
                    )
                    if table:
                        tables.append(table)
                
                i = j
            else:
                i += 1
        
        return tables
    
    def _detect_tab_delimited_tables(self, text: str) -> List[DetectedTable]:
        """Detect tables with tab delimiters."""
        tables = []
        
        lines = text.split('\n')
        i = 0
        while i < len(lines):
            if lines[i].count('\t') >= self.min_columns - 1:
                table_lines = [lines[i]]
                j = i + 1
                while j < len(lines) and lines[j].count('\t') >= self.min_columns - 1:
                    table_lines.append(lines[j])
                    j += 1
                
                if len(table_lines) >= self.min_rows:
                    table = self._parse_delimited_table(
                        table_lines, delimiter='\t', start_offset=sum(len(l) + 1 for l in lines[:i])
                    )
                    if table:
                        tables.append(table)
                
                i = j
            else:
                i += 1
        
        return tables
    
    def _detect_markdown_tables(self, text: str) -> List[DetectedTable]:
        """Detect Markdown-format tables."""
        tables = []
        
        # Markdown table pattern: | cells | ... |
        markdown_re = re.compile(r'^\|.+\|\s*\n\|\s*[-:]+\s*(?:\|\s*[-:]+\s*)*\|', re.MULTILINE)
        
        for match in markdown_re.finditer(text):
            start = match.start()
            # Find end of table (next non-table line)
            end = match.end()
            i = end
            while i < len(text):
                if text[i:i+1] == '\n':
                    next_line_start = i + 1
                    next_line_end = text.find('\n', next_line_start)
                    if next_line_end == -1:
                        next_line_end = len(text)
                    next_line = text[next_line_start:next_line_end]
                    
                    if next_line.startswith('|'):
                        end = next_line_end
                        i = next_line_end
                    else:
                        break
                else:
                    i += 1
            
            table_text = text[start:end]
            table = self._parse_markdown_table(table_text, start_offset=start)
            if table:
                tables.append(table)
        
        return tables
    
    def _parse_delimited_table(
        self,
        lines: List[str],
        delimiter: str,
        start_offset: int,
    ) -> Optional[DetectedTable]:
        """Parse a delimited table (pipe or tab)."""
        rows = []
        for line in lines:
            cells = [c.strip() for c in line.split(delimiter)]
            # Remove empty leading/trailing cells (from leading/trailing delimiters)
            cells = [c for c in cells if c]
            if len(cells) >= self.min_columns:
                rows.append(cells)
        
        if len(rows) < self.min_rows:
            return None
        
        # Normalize column count
        col_count = max(len(row) for row in rows)
        for row in rows:
            while len(row) < col_count:
                row.append("")
        
        table = DetectedTable(
            start_char=start_offset,
            end_char=start_offset + sum(len(l) + 1 for l in lines),
            raw_text='\n'.join(lines),
            rows=rows,
            column_count=col_count,
            delimiter=delimiter,
            confidence=0.9,
        )
        
        table.infer_column_types()
        return table
    
    def _parse_markdown_table(
        self,
        table_text: str,
        start_offset: int,
    ) -> Optional[DetectedTable]:
        """Parse a Markdown-format table."""
        lines = table_text.strip().split('\n')
        if len(lines) < 3:
            return None
        
        # Parse header and separator
        header_line = lines[0]
        header = [c.strip() for c in header_line.split('|')[1:-1]]  # Skip outer pipes
        
        rows = []
        for line in lines[2:]:  # Skip header and separator
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if cells and any(cells):  # Non-empty row
                rows.append(cells)
        
        if not rows:
            return None
        
        col_count = len(header)
        table = DetectedTable(
            start_char=start_offset,
            end_char=start_offset + len(table_text),
            raw_text=table_text,
            rows=rows,
            column_count=col_count,
            header=header,
            has_header=True,
            delimiter="|",
            confidence=0.95,
        )
        
        table.infer_column_types()
        return table
    
    def _deduplicate_overlapping(self, tables: List[DetectedTable]) -> List[DetectedTable]:
        """Remove overlapping table detections (keep highest confidence)."""
        if not tables:
            return []
        
        result = [tables[0]]
        for table in tables[1:]:
            # Check if this table overlaps with any kept table
            overlaps = False
            for kept in result:
                if (table.start_char < kept.end_char and table.end_char > kept.start_char):
                    overlaps = True
                    # Keep the higher-confidence one
                    if table.confidence > kept.confidence:
                        result.remove(kept)
                        result.append(table)
                    break
            
            if not overlaps:
                result.append(table)
        
        return result
    
    def table_to_prose(self, table: DetectedTable) -> str:
        """
        Convert a detected table to prose sentences.
        
        Args:
            table: DetectedTable object
            
        Returns:
            prose string, or empty if unable to convert
        """
        if not table.rows or not table.column_types:
            return ""
        
        sentences = []
        
        for row in table.rows:
            # Extract fields by column type
            species_field = ""
            location_field = ""
            date_field = ""
            habitat_field = ""
            notes_field = ""
            
            for col_idx, cell in enumerate(row):
                if col_idx >= len(table.column_types):
                    break
                
                col_type = table.column_types[col_idx]
                if col_type == ColumnType.SPECIES:
                    species_field = cell
                elif col_type == ColumnType.LOCATION:
                    location_field = cell
                elif col_type == ColumnType.DATE:
                    date_field = cell
                elif col_type == ColumnType.HABITAT:
                    habitat_field = cell
                elif col_type == ColumnType.NOTES:
                    notes_field = cell
            
            # Build prose sentence
            if species_field:
                sentence = f"{species_field}"
                
                if location_field and date_field:
                    sentence += f" was found at {location_field} in {date_field}"
                elif location_field:
                    sentence += f" was found at {location_field}"
                elif date_field:
                    sentence += f" was observed in {date_field}"
                
                if habitat_field:
                    sentence += f" in {habitat_field} habitat"
                
                if notes_field:
                    sentence += f" ({notes_field})"
                
                sentence += "."
                sentences.append(sentence)
        
        return " ".join(sentences)
    
    def table_to_expanded_prose(
        self,
        table: DetectedTable,
        use_article: bool = True,
    ) -> str:
        """
        Convert table to more verbose/natural prose.
        
        Args:
            table: DetectedTable
            use_article: Use "The", "A" for readability
            
        Returns:
            expanded prose string
        """
        if not table.rows or not table.column_types:
            return ""
        
        sentences = []
        species_list = []
        
        for row in table.rows:
            species_field = ""
            location_field = ""
            date_field = ""
            habitat_field = ""
            
            for col_idx, cell in enumerate(row):
                if col_idx >= len(table.column_types):
                    break
                
                col_type = table.column_types[col_idx]
                if col_type == ColumnType.SPECIES:
                    species_field = cell
                elif col_type == ColumnType.LOCATION:
                    location_field = cell
                elif col_type == ColumnType.DATE:
                    date_field = cell
                elif col_type == ColumnType.HABITAT:
                    habitat_field = cell
            
            if species_field:
                article = "The" if use_article else ""
                
                parts = [f"{article} {species_field}".strip()]
                
                if habitat_field:
                    parts.append(f"inhabits {habitat_field}")
                
                if location_field:
                    parts.append(f"found at {location_field}")
                
                if date_field:
                    parts.append(f"recorded in {date_field}")
                
                sentence = ", ".join(parts) + "."
                sentences.append(sentence)
                species_list.append(species_field)
        
        if sentences:
            summary = f"This table documents {len(species_list)} records. "
            summary += " ".join(sentences)
            return summary
        
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY: Standalone function
# ─────────────────────────────────────────────────────────────────────────────

def dematrix_pdf_text(text: str) -> str:
    """
    Convenience function: de-matrix tables in PDF text.
    
    Args:
        text: Extracted PDF text (e.g., from Docling)
        
    Returns:
        cleaned text with tables converted to prose
    """
    agent = TablePreprocessorAgent()
    return agent.process_document(text)
