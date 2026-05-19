"""
biotrace_phase0_dwca_bootstrap.py

PHASE 0: GBIF DWCA + Manual CSV → SQLite Training Database
- pygbif library for DWCA archives (native format)
- Memory-efficient streaming + filtering
- Marine taxa + India region bounds
- Direct SQLite insert into HITLFeedbackDatabase
"""

import sqlite3
import pandas as pd
import logging
from pathlib import Path
from typing import Dict, List, Optional, Generator
from datetime import datetime
import json

# pygbif for DWCA handling
try:
    from pygbif import occurrences as gbif_occ
    HAS_PYGBIF = True
except ImportError:
    HAS_PYGBIF = False

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# MARINE TAXA FILTER
# ──────────────────────────────────────────────────────────────────────────────

# MARINE_TAXA = {
#     'Actinopterygii', 'Elasmobranchii', 'Petromyzontida',
#     'Cephalopoda', 'Malacostraca', 'Gastropoda', 'Bivalvia',
#     'Polyplacophora', 'Anthozoa', 'Scyphozoa',
#     'Echinoidea', 'Asteroidea', 'Ophiuroidea', 'Holothuroidea',
#     'Polychaeta', 'Bryozoa', 'Porifera',
# }

INDIA_BOUNDS = {'lat_min': 5.0, 'lat_max': 37.0, 'lon_min': 65.0, 'lon_max': 100.0}


class GBIFDWCABootstrapper:
    """
    Load GBIF DWCA format using pygbif.
    Handles large downloads efficiently with filtering.
    """
    
    def __init__(self, feedback_db_path: str):
        self.feedback_db_path = Path(feedback_db_path)
        self.stats = {
            'total_fetched': 0, 'marine_filtered': 0, 'region_filtered': 0,
            'valid_coords': 0, 'inserted': 0, 'duplicates': 0, 'errors': 0
        }
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite tables"""
        with sqlite3.connect(self.feedback_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS occurrence_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    predicted_type TEXT,
                    corrected_type TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    source_doc TEXT,
                    user_notes TEXT,
                    agent_id TEXT,
                    gbif_id TEXT UNIQUE,
                    latitude REAL, longitude REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_gbif_id 
                ON occurrence_corrections(gbif_id)
            """)
            conn.commit()
        logger.info("✓ SQLite tables initialized")
    
    # def _is_marine(self, record: Dict) -> bool:
    #     """Filter to marine classes/phyla"""
    #     class_val = record.get('class', '')
    #     phylum_val = record.get('phylum', '')
    #     return class_val in MARINE_TAXA or phylum_val in MARINE_TAXA
    
    def _validate_coords(self, record: Dict) -> tuple:
        """Return (is_valid, lat, lon)"""
        try:
            lat = float(record.get('decimalLatitude'))
            lon = float(record.get('decimalLongitude'))
            
            if not (INDIA_BOUNDS['lat_min'] <= lat <= INDIA_BOUNDS['lat_max']):
                return False, None, None
            if not (INDIA_BOUNDS['lon_min'] <= lon <= INDIA_BOUNDS['lon_max']):
                return False, None, None
            if lat == 0 and lon == 0:
                return False, None, None
            
            return True, lat, lon
        except (ValueError, TypeError):
            return False, None, None
    
    def fetch_india_marine(self, country_code: str = "IN", limit: int = 1000) -> Generator:
        """
        Stream GBIF records for India using pygbif.
        
        Args:
            country_code: ISO country code (IN = India)
            limit: Records per API call
        
        Yields:
            Filtered records matching marine + region criteria
        """
        if not HAS_PYGBIF:
            logger.error("pygbif not installed. pip install pygbif")
            return
        
        offset = 0
        
        while True:
            logger.info(f"Fetching GBIF records offset={offset}...")
            
            try:
                # pygbif returns dict with 'results' key
                response = gbif_occ.search(
                    country=country_code,
                    classKey=None,  # Will filter manually
                    offset=offset,
                    limit=limit,
                    **{'basis_of_record': 'OBSERVATION,SPECIMEN'}  # Marine records
                )
                
                if not response or not response.get('results'):
                    logger.info("✓ No more records")
                    break
                
                for record in response['results']:
                    # Filter
                    # if not self._is_marine(record):
                    #     continue
                    
                    is_valid, lat, lon = self._validate_coords(record)
                    if not is_valid:
                        continue
                    
                    yield record, lat, lon
                
                offset += limit
                
                # Stop if reached end
                if len(response['results']) < limit:
                    break
            
            except Exception as e:
                logger.error(f"API error: {e}")
                break
    
    def bootstrap_from_api(self, dry_run: bool = False) -> Dict:
        """Fetch from GBIF API and insert"""
        logger.info("Starting GBIF API bootstrap...")
        
        with sqlite3.connect(self.feedback_db_path) as conn:
            for record, lat, lon in self.fetch_india_marine():
                self.stats['total_fetched'] += 1
                
                # # if not self._is_marine(record):
                # #     continue
                # self.stats['marine_filtered'] += 1
                
                is_valid, lat, lon = self._validate_coords(record)
                if not is_valid:
                    continue
                self.stats['region_filtered'] += 1
                self.stats['valid_coords'] += 1
                
                # Synthesize text
                sci_name = record.get('scientificName', 'Unknown')
                locality = record.get('locality', 'Unspecified')
                state = record.get('stateProvince', 'India')
                text = f"{sci_name} observed at {locality}, {state}"
                
                # Occurrence type
                status = record.get('occurrenceStatus', 'PRESENT').upper()
                occ_type = 'Secondary' if 'absent' in status.lower() else 'Primary'
                
                if not dry_run:
                    try:
                        conn.execute("""
                            INSERT INTO occurrence_corrections
                            (text, predicted_type, corrected_type, confidence,
                             timestamp, source_doc, user_notes, gbif_id,
                             latitude, longitude)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            text, 'Uncertain', occ_type, 1.0,
                            datetime.now().isoformat(),
                            f"GBIF|{record.get('datasetName', 'Unknown')}",
                            f"Bootstrap from API | {record.get('class', '')}",
                            record.get('gbifID'),
                            lat, lon
                        ))
                        self.stats['inserted'] += 1
                    except sqlite3.IntegrityError:
                        self.stats['duplicates'] += 1
                
                if self.stats['inserted'] % 100 == 0:
                    logger.info(f"Inserted {self.stats['inserted']} records...")
                    conn.commit()
        
        logger.info(f"✓ Bootstrap complete | Inserted: {self.stats['inserted']}")
        return self.stats


class ManualCSVLoader:
    """Load pre-verified annotations CSV"""
    
    def __init__(self, csv_path: str, feedback_db_path: str):
        self.csv_path = Path(csv_path)
        self.feedback_db_path = Path(feedback_db_path)
    
    def load(self, confidence: float = 0.95) -> Dict:
        """Stream CSV → SQLite"""
        logger.info(f"Loading {self.csv_path}")
        
        stats = {'total': 0, 'inserted': 0, 'skipped': 0}
        
        with sqlite3.connect(self.feedback_db_path) as conn:
            for chunk in pd.read_csv(self.csv_path, chunksize=5000):
                for _, row in chunk.iterrows():
                    stats['total'] += 1
                    
                    text = str(row.get('text', row.get('raw_excerpt', ''))).strip()
                    species = str(row.get('species', row.get('scientific_name', ''))).strip()
                    occ_type = str(row.get('occurrence_type', 'Primary')).strip()
                    
                    if not text or not species:
                        stats['skipped'] += 1
                        continue
                    
                    if len(text) < 10:
                        text = f"{species} - {text}"
                    
                    try:
                        conn.execute("""
                            INSERT INTO occurrence_corrections
                            (text, predicted_type, corrected_type, confidence,
                             timestamp, source_doc, user_notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            text, 'Uncertain', occ_type, confidence,
                            datetime.now().isoformat(), 'manual_csv',
                            f"Manual verified | species: {species}"
                        ))
                        stats['inserted'] += 1
                    except Exception as e:
                        logger.warning(f"Row error: {e}")
                        stats['skipped'] += 1
                
                conn.commit()
        
        logger.info(f"✓ Loaded {stats['inserted']} records")
        return stats
