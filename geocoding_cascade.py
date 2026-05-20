# """
# geocoding_cascade.py  —  BioTrace v3.1
# ────────────────────────────────────────────────────────────────────────────
# Unified geocoding pipeline for occurrence records.

# Four tools in strict priority order:
#   1. coord_utils.parse_dms()     — DMS/OCR strings → decimal degrees (always first)
#   2. IndianPincodeGeocoder       — 5-stage fuzzy Indian place matching
#   3. GeoNames IN SQLite          — fast local lookup
#   4. NominatimEnrichedGeocoder   — district+state-qualified, network fallback

# After any coordinate is filled:
#   coord_utils.validate_occurrence_coordinates() checks India bbox,
#   state-level bbox, ocean context, and pincode mismatch.

# Usage
# -----
#     geo = GeocodingCascade(
#         geonames_db   = "biodiversity_data/geonames_india.db",
#         pincode_txt   = "biodiversity_data/IN_pin.txt",
#         use_nominatim = True,
#     )
#     occurrences = geo.geocode_batch(occurrences)
# """
# from __future__ import annotations
# import logging, os, sqlite3
# from typing import Optional
# logger = logging.getLogger("biotrace.geocoding")


# def _to_float(v) -> Optional[float]:
#     if v is None: return None
#     try:
#         f = float(str(v).strip())
#         return None if str(v).strip() in ("0","") else f
#     except (ValueError, TypeError):
#         return None

# def _has_coords(occ: dict) -> bool:
#     return _to_float(occ.get("decimalLatitude")) is not None \
#        and _to_float(occ.get("decimalLongitude")) is not None


# def _resolve_occurrence_table(conn: sqlite3.Connection) -> str:
#     """
#     Prefer the current v4 schema while remaining backward-compatible with
#     older databases that still use the legacy `occurrences` table.
#     """
#     rows = conn.execute(
#         "SELECT name FROM sqlite_master WHERE type='table'"
#     ).fetchall()
#     names = {row[0] for row in rows}
#     if "occurrences_v4" in names:
#         return "occurrences_v4"
#     if "occurrences" in names:
#         return "occurrences"
#     raise sqlite3.OperationalError(
#         "No occurrence table found (expected `occurrences_v4` or `occurrences`)."
#     )


# class GeocodingCascade:
#     """Four-tool geocoding cascade for BioTrace occurrence records."""

#     def __init__(
#         self,
#         geonames_db:    str  = "",
#         pincode_txt:    str  = "",
#         pincode_state:  Optional[str] = None,
#         use_nominatim:  bool = False,
#         nominatim_agent:str  = "BioTrace_v3_biodiversity_extractor",
#     ):
#         self.geonames_db   = geonames_db
#         self.use_nominatim = use_nominatim

#         # Tool 2 — IndianPincodeGeocoder
#         self._pincode = None
#         if pincode_txt and os.path.exists(pincode_txt):
#             try:
#                 from pincode_geocoder import IndianPincodeGeocoder
#                 self._pincode = IndianPincodeGeocoder(pincode_txt,
#                                                       fuzzy_threshold=80.0,
#                                                       state_filter=pincode_state)
#                 logger.info("[geocoding] PincodeGeocoder ready (%s)", pincode_txt)
#             except ImportError:
#                 logger.warning("[geocoding] pincode_geocoder unavailable — pip install rapidfuzz")
#             except Exception as exc:
#                 logger.warning("[geocoding] PincodeGeocoder init: %s", exc)

#         # Tool 4 — NominatimEnrichedGeocoder
#         self._nominatim = None
#         if use_nominatim:
#             try:
#                 from nominatim_geocoder import NominatimEnrichedGeocoder
#                 self._nominatim = NominatimEnrichedGeocoder(
#                     geonames_db_path=geonames_db,
#                     user_agent=nominatim_agent,
#                 )
#                 logger.info("[geocoding] NominatimGeocoder ready")
#             except ImportError:
#                 logger.warning("[geocoding] nominatim_geocoder unavailable — pip install geopy")
#             except Exception as exc:
#                 logger.warning("[geocoding] Nominatim init: %s", exc)

#     # ─────────────────────────────────────────────────────────────────────────
#     #  Tool 1 · DMS string parsing
#     # ─────────────────────────────────────────────────────────────────────────
#     @staticmethod
#     def _parse_dms(occ: dict) -> dict:
#         try:
#             from coord_utils import parse_dms
#         except ImportError:
#             return occ
#         for field in ("decimalLatitude","decimalLongitude"):
#             val = occ.get(field)
#             if isinstance(val, str) and val.strip():
#                 parsed = parse_dms(val.strip())
#                 if parsed is not None:
#                     occ[field] = parsed
#         return occ

#     # ─────────────────────────────────────────────────────────────────────────
#     #  Tool 3 · GeoNames IN SQLite
#     # ─────────────────────────────────────────────────────────────────────────
#     def _geonames(self, locality: str) -> Optional[tuple[float,float]]:
#         if not locality or not self.geonames_db or not os.path.exists(self.geonames_db):
#             return None
#         try:
#             conn = sqlite3.connect(self.geonames_db, check_same_thread=False)
#             res  = conn.execute(
#                 """SELECT latitude,longitude FROM geonames
#                    WHERE (name=? OR asciiname=? OR alternatenames LIKE ?)
#                    AND country_code='IN'
#                    ORDER BY CASE feature_class WHEN 'P' THEN 1 WHEN 'A' THEN 2 ELSE 3 END,
#                    CAST(population AS INTEGER) DESC LIMIT 1""",
#                 (locality, locality, f"%{locality}%")
#             ).fetchone()
#             conn.close()
#             return (float(res[0]), float(res[1])) if res else None
#         except Exception as exc:
#             logger.debug("[GeoNames] %s → %s", locality, exc)
#         return None

#     # ─────────────────────────────────────────────────────────────────────────
#     #  Coordinate validation
#     # ─────────────────────────────────────────────────────────────────────────
#     @staticmethod
#     def _validate(occ: dict) -> dict:
#         try:
#             from coord_utils import validate_occurrence_coordinates
#             return validate_occurrence_coordinates(occ)
#         except ImportError:
#             return occ
#         except Exception as exc:
#             logger.debug("[validate] %s", exc)
#             return occ

#     # ─────────────────────────────────────────────────────────────────────────
#     #  Public: geocode_batch
#     # ─────────────────────────────────────────────────────────────────────────
#     def geocode_batch(self, occurrences: list[dict]) -> list[dict]:
#         """
#         Run the 4-tool cascade on every record in the list.

#         geocodingSource values set by each tool:
#           "LLM"               — coordinates from LLM extraction
#           "IN_Pincode_*"      — pincode geocoder (match_type + score)
#           "GeoNames_IN"       — GeoNames SQLite
#           "Nominatim"         — Nominatim
#         """
#         if not occurrences: return occurrences
#         result = []

#         for occ in occurrences:
#             if not isinstance(occ, dict):
#                 result.append(occ); continue

#             # Step 1: Parse DMS strings
#             occ = self._parse_dms(occ)

#             # If LLM already provided valid numeric coords → validate and pass through
#             if _has_coords(occ):
#                 occ.setdefault("geocodingSource","LLM")
#                 occ = self._validate(occ)
#                 result.append(occ); continue

#             locality = str(occ.get("verbatimLocality","")).strip()

#             # Step 2: Pincode geocoder
#             if self._pincode and locality:
#                 try:
#                     gr = self._pincode.geocode(locality)
#                     if gr and gr.latitude is not None:
#                         occ["decimalLatitude"]  = gr.latitude
#                         occ["decimalLongitude"] = gr.longitude
#                         occ["geocodingSource"]  = f"IN_Pincode_{gr.match_type}_{gr.score:.0f}"
#                         occ = self._validate(occ)
#                         result.append(occ); continue
#                 except Exception as exc:
#                     logger.debug("[pincode] %s",exc)

#             # Step 3: GeoNames IN
#             if locality:
#                 coords = self._geonames(locality)
#                 if coords:
#                     occ["decimalLatitude"]  = coords[0]
#                     occ["decimalLongitude"] = coords[1]
#                     occ["geocodingSource"]  = "GeoNames_IN"
#                     occ = self._validate(occ)
#                     result.append(occ); continue

#             result.append(occ)

#         # Step 4: Nominatim (batch, deduplicated per unique locality)
#         if self._nominatim:
#             missing = [o for o in result
#                        if isinstance(o,dict) and not _has_coords(o) and o.get("verbatimLocality")]
#             if missing:
#                 logger.info("[geocoding] Nominatim: %d remaining unresolved", len(missing))
#                 try:
#                     geocoded = self._nominatim.geocode_missing(missing)
#                     geocoded = [self._validate(o) for o in geocoded]
#                     id_map   = {id(o): o for o in geocoded}
#                     result   = [id_map.get(id(o), o) for o in result]
#                 except Exception as exc:
#                     logger.warning("[geocoding] Nominatim batch: %s", exc)

#         filled = sum(1 for o in result if isinstance(o,dict) and _has_coords(o))
#         logger.info("[geocoding] %d/%d records geocoded", filled, len(result))
#         return result

#     def geocode_single(self, occ: dict) -> dict:
#         return self.geocode_batch([occ])[0]

#     # ─────────────────────────────────────────────────────────────────────────
#     #  Batch DB update (for "Geocode Missing" button)
#     # ─────────────────────────────────────────────────────────────────────────
#     def batch_geocode_db(self, meta_db_path: str, progress_callback=None) -> int:
#         conn = sqlite3.connect(meta_db_path, check_same_thread=False)
#         table = _resolve_occurrence_table(conn)
#         rows = conn.execute(
#             f"""SELECT id,verbatimLocality FROM {table}
#                WHERE (decimalLatitude IS NULL OR decimalLongitude IS NULL)
#                AND verbatimLocality IS NOT NULL AND verbatimLocality != ''
#                AND validationStatus != 'rejected'"""
#         ).fetchall()
#         if not rows:
#             conn.close(); return 0
#         logger.info("[geocoding/db] %d rows to geocode", len(rows))
#         updated = 0
#         for i,(row_id,vl) in enumerate(rows):
#             occ = {"verbatimLocality":vl,"decimalLatitude":None,"decimalLongitude":None}
#             occ = self.geocode_single(occ)
#             lat = _to_float(occ.get("decimalLatitude"))
#             lon = _to_float(occ.get("decimalLongitude"))
#             if lat is not None and lon is not None:
#                 conn.execute(
#                     f"UPDATE {table} SET decimalLatitude=?,decimalLongitude=?,geocodingSource=? WHERE id=?",
#                     (lat,lon,occ.get("geocodingSource",""),row_id))
#                 updated += 1
#             if updated % 50 == 0 and updated > 0: conn.commit()
#             if progress_callback: progress_callback(i+1,len(rows))
#         conn.commit(); conn.close()
#         logger.info("[geocoding/db] %d/%d updated", updated, len(rows))
#         return updated


"""
geocoding_cascade.py  —  BioTrace v5.6
────────────────────────────────────────────────────────────────────────────
Unified geocoding pipeline for occurrence records.

Five tools in strict priority order:
  1. coord_utils.parse_dms()     — DMS/OCR strings → decimal degrees
  2. GPKG Native                 — Local spatial package (Fuzzy Phonetic Search)
  3. IndianPincodeGeocoder       — 5-stage fuzzy Indian place matching
  4. GeoNames IN SQLite          — fast local lookup
  5. NominatimEnrichedGeocoder   — district+state-qualified, network fallback

After any coordinate is filled:
  coord_utils.validate_occurrence_coordinates() checks India bbox,
  state-level bbox, ocean context, and pincode mismatch.
"""
import sqlite3
import os
import logging
from typing import Optional
from rapidfuzz import process, fuzz

logger = logging.getLogger("biotrace.geocoding")


logger = logging.getLogger("biotrace.geocoding")

def _to_float(v) -> Optional[float]:
    if v is None: return None
    try:
        f = float(str(v).strip())
        return None if str(v).strip() in ("0","") else f
    except (ValueError, TypeError):
        return None

def _has_coords(occ: dict) -> bool:
    return _to_float(occ.get("decimalLatitude")) is not None \
       and _to_float(occ.get("decimalLongitude")) is not None

def _resolve_occurrence_table(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {row[0] for row in rows}
    if "occurrences_v4" in names: return "occurrences_v4"
    if "occurrences" in names: return "occurrences"
    raise sqlite3.OperationalError("No occurrence table found.")

class GeocodingCascade:
    """Five-tool geocoding cascade for BioTrace occurrence records."""

    def __init__(self, geonames_db="", pincode_txt="",
                 pincode_state=None, use_nominatim=False,
                 nominatim_agent="BioTrace", gpkg_path="biodiversity_data/destination_gpkg_folder/combined_layers.gpkg",
                 hierarchy_db="biodiversity_data/locality_hierarchy.db"):

        self.geonames_db = geonames_db
        self.use_nominatim = use_nominatim
        self.gpkg_path = gpkg_path
        self.hierarchy_db = hierarchy_db



        # ── Tool 3 · Pincode
        self._pincode = None
        if pincode_txt and os.path.exists(pincode_txt):
            try:
                from pincode_geocoder import IndianPincodeGeocoder
                self._pincode = IndianPincodeGeocoder(pincode_txt, fuzzy_threshold=80.0, state_filter=pincode_state)
            except Exception as exc:
                logger.warning("[geocoding] PincodeGeocoder init: %s", exc)

        # ── Tool 5 · Nominatim Fallback
        self._nominatim = None
        if use_nominatim:
            try:
                from nominatim_geocoder import NominatimEnrichedGeocoder
                self._nominatim = NominatimEnrichedGeocoder(geonames_db_path=geonames_db, user_agent=nominatim_agent)
            except Exception as exc:
                logger.warning("[geocoding] Nominatim init: %s", exc)

        # ── Tool 2 · SQLite-Backed GPKG Fuzzy Search
        self.village_records = []

        if self.hierarchy_db and os.path.exists(self.hierarchy_db):
            try:
                # Load the lightweight SQLite map instantly
                conn = sqlite3.connect(self.hierarchy_db)
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT village, district, state, layer_name FROM villages").fetchall()

                # Cache as dicts for fast access
                self.village_records = [dict(r) for r in rows]
                self.village_names = [r["village"] for r in self.village_records]
                conn.close()
                logger.info(f"[geocoding] ✓ SQLite GPKG index loaded ({len(self.village_records)} villages).")
            except Exception as exc:
                logger.warning(f"[geocoding] SQLite hierarchy init failed: {exc}")

    def _detect_gpkg_columns(self) -> dict:
        """Dynamically detect attribute columns mapping to village/district."""
        if self.gpkg_gdf is None: return {}
        cols = {c.lower(): c for c in self.gpkg_gdf.columns}
        return {
            'village': next((cols[c] for c in ['village', 'name', 'vill_name', 'gn_name'] if c in cols), None),
            'district': next((cols[c] for c in ['district', 'district_name'] if c in cols), None),
            'state': next((cols[c] for c in ['state', 'state_name'] if c in cols), None)
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  GPKG Core Logic: Fuzzy Search & Spatial Validation
    # ─────────────────────────────────────────────────────────────────────────
    def _gpkg_fuzzy_search(self, locality_string: str) -> Optional[dict]:
        """
        Fuzzy searches the SQLite DB, uses enriched context for disambiguation,
        and extracts geometry from GPKG lazily.
        """
        if not self.village_records or not locality_string:
            return None

        # 1. Fuzzy match the core string against village names
        # We use token_set_ratio so "Narara Gujarat" matches "Narara" perfectly
        matches = process.extract(
            locality_string,
            self.village_names,
            scorer=fuzz.token_set_ratio,
            score_cutoff=85,
            limit=10
        )

        if not matches:
            return None

        best_score = matches[0][1]
        top_matches = [m for m in matches if m[1] == best_score]

        selected_record = None
        source_str = f"GPKG_Fuzzy_{best_score:.0f}"

        # 2. DISAMBIGUATION: If multiple villages have the same score
        if len(top_matches) > 1:
            loc_lower = locality_string.lower()

            # Check if the enriched locality_string contains the state or district of the match
            for match in top_matches:
                idx = match[2]
                record = self.village_records[idx]
                state = str(record.get('state', '')).lower()
                district = str(record.get('district', '')).lower()

                if (state and state in loc_lower) or (district and district in loc_lower):
                    selected_record = record
                    source_str += "_Context_Resolved"
                    break

            # If context couldn't resolve it, flag for Human-in-the-Loop
            if not selected_record:
                selected_record = self.village_records[top_matches[0][2]]
                source_str += "_Multiple_Matches_HITL"
        else:
            selected_record = self.village_records[top_matches[0][2]]

        # 3. LAZY SPATIAL LOAD: Fetch just the polygon for the winning record
        if selected_record and self.gpkg_path and os.path.exists(self.gpkg_path):
            try:
                import geopandas as gpd
                layer = selected_record['layer_name']
                v_name = selected_record['village'].replace("'", "''") # Escape SQL quotes

                # Auto-detect column name based on layer
                v_col = 'village' if not layer.endswith('_soi_ar') else 'name' # Adjust based on your GPKG

                # Fetch only this exact village from the heavy GPKG
                sql = f"SELECT geometry FROM {layer} WHERE {v_col} = '{v_name}' LIMIT 1"
                matched_gdf = gpd.read_file(self.gpkg_path, engine="pyogrio", sql=sql)

                if not matched_gdf.empty:
                    centroid = matched_gdf.iloc[0].geometry.centroid
                    return {
                        "decimalLatitude": float(centroid.y),
                        "decimalLongitude": float(centroid.x),
                        "geocodingSource": source_str,
                        "polygon_matched": selected_record['village']
                    }
            except Exception as e:
                logger.warning(f"[geocoding] Failed lazy spatial load for GPKG: {e}")

        return None

    def _nearest_gpkg_village(self, lat: float, lon: float, k: int = 1, max_dist_km: float = 10) -> Optional[dict]:
        if self.gpkg_gdf is None: return None
        import geopandas as gpd
        from shapely.geometry import Point

        pt_proj = gpd.GeoSeries([Point(lon, lat)], crs='EPSG:4326').to_crs('EPSG:3857').iloc[0]
        gdf_proj = self.gpkg_gdf.to_crs('EPSG:3857')

        nearby = gdf_proj[gdf_proj.geometry.intersects(pt_proj.buffer(max_dist_km * 1000))]
        if nearby.empty: return None

        distances = nearby.geometry.distance(pt_proj)
        nearest_idx = distances.nsmallest(k).index[0]
        v_col = self.gpkg_cols.get('village')

        return {
            "village": self.gpkg_gdf.loc[nearest_idx, v_col] if v_col else "Unknown",
            "distance_km": distances[nearest_idx] / 1000
        }

    def _validate_with_gpkg(self, occ: dict) -> dict:
        """Reverse geocode validation: Check if coords fall within GPKG polygon."""
        if self.gpkg_gdf is None: return occ
        lat, lon = _to_float(occ.get("decimalLatitude")), _to_float(occ.get("decimalLongitude"))
        if lat is None or lon is None: return occ

        from shapely.geometry import Point
        pt = Point(lon, lat)
        candidates = list(self.gpkg_sindex.intersection(pt.bounds))

        for idx in candidates:
            if self.gpkg_gdf.iloc[idx].geometry.contains(pt):
                v_col = self.gpkg_cols.get('village')
                occ["geocodingSource"] = f"{occ.get('geocodingSource', '')}+GPKG_Valid"
                occ["polygon_matched"] = self.gpkg_gdf.iloc[idx][v_col] if v_col else "Unknown"
                return occ

        nearest = self._nearest_gpkg_village(lat, lon, k=1)
        if nearest:
            occ["geocodingSource"] = f"{occ.get('geocodingSource', '')}+GPKG_Nearest({nearest['distance_km']:.1f}km)"
            occ["polygon_matched"] = nearest["village"]
        else:
            occ["geocodingSource"] = f"{occ.get('geocodingSource', '')}+GPKG_Out_of_Bounds"

        return occ

    # ─────────────────────────────────────────────────────────────────────────
    #  Tool 1 · DMS string parsing
    # ─────────────────────────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    #  Coordinate validation
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate(occ: dict) -> dict:
        try:
            from coord_utils import validate_occurrence_coordinates
            return validate_occurrence_coordinates(occ)
        except Exception: return occ

    # ─────────────────────────────────────────────────────────────────────────
    #  Public: geocode_batch
    # ─────────────────────────────────────────────────────────────────────────
    def geocode_batch(self, occurrences: list[dict]) -> list[dict]:
        if not occurrences: return occurrences
        result = []

        for occ in occurrences:
            if not isinstance(occ, dict):
                result.append(occ); continue

            # Tool 1: Parse DMS strings
            occ = self._parse_dms(occ)

            if _has_coords(occ):
                occ.setdefault("geocodingSource","LLM")
                occ = self._validate_with_gpkg(occ)
                occ = self._validate(occ)
                result.append(occ); continue

            # locality = str(occ.get("verbatimLocality","")).strip()
            locality = str(occ.get("_geocodingLocality") or occ.get("verbatimLocality", "")).strip()

            # Tool 2: GPKG Native Fuzzy Search (PRIORITY)
            if locality and self.gpkg_gdf is not None:
                gpkg_res = self._gpkg_fuzzy_search(locality)
                if gpkg_res:
                    occ["decimalLatitude"]  = gpkg_res["decimalLatitude"]
                    occ["decimalLongitude"] = gpkg_res["decimalLongitude"]
                    occ["geocodingSource"]  = gpkg_res["geocodingSource"]
                    occ["polygon_matched"]  = gpkg_res["polygon_matched"]
                    occ = self._validate(occ)
                    result.append(occ); continue

            # Tool 3: Pincode geocoder
            if self._pincode and locality:
                try:
                    gr = self._pincode.geocode(locality)
                    if gr and gr.latitude is not None:
                        occ["decimalLatitude"], occ["decimalLongitude"] = gr.latitude, gr.longitude
                        occ["geocodingSource"] = f"IN_Pincode_{gr.match_type}_{gr.score:.0f}"
                        occ = self._validate_with_gpkg(occ)
                        occ = self._validate(occ)
                        result.append(occ); continue
                except Exception: pass

            # Tool 4: GeoNames IN
            if locality and self.geonames_db and os.path.exists(self.geonames_db):
                try:
                    conn = sqlite3.connect(self.geonames_db, check_same_thread=False)
                    res = conn.execute(
                        "SELECT latitude,longitude FROM geonames WHERE (name=? OR asciiname=? OR alternatenames LIKE ?) AND country_code='IN' ORDER BY CASE feature_class WHEN 'P' THEN 1 WHEN 'A' THEN 2 ELSE 3 END, CAST(population AS INTEGER) DESC LIMIT 1",
                        (locality, locality, f"%{locality}%")
                    ).fetchone()
                    conn.close()
                    if res:
                        occ["decimalLatitude"], occ["decimalLongitude"] = float(res[0]), float(res[1])
                        occ["geocodingSource"] = "GeoNames_IN"
                        occ = self._validate_with_gpkg(occ)
                        occ = self._validate(occ)
                        result.append(occ); continue
                except Exception: pass

            result.append(occ)

        # Tool 5: Nominatim Batch Fallback + GPKG Validation
        if self._nominatim:
            missing = [o for o in result if isinstance(o,dict) and not _has_coords(o) and o.get("verbatimLocality")]
            if missing:
                logger.info("[geocoding] Nominatim: %d remaining unresolved", len(missing))
                try:
                    geocoded = self._nominatim.geocode_missing(missing)
                    geocoded = [self._validate_with_gpkg(o) for o in geocoded]
                    geocoded = [self._validate(o) for o in geocoded]

                    id_map = {id(o): o for o in geocoded}
                    result = [id_map.get(id(o), o) for o in result]
                except Exception as exc:
                    logger.warning("[geocoding] Nominatim batch: %s", exc)

        filled = sum(1 for o in result if isinstance(o,dict) and _has_coords(o))
        logger.info("[geocoding] %d/%d records geocoded", filled, len(result))
        return result

    def geocode_single(self, occ: dict) -> dict:
        return self.geocode_batch([occ])[0]

    def batch_geocode_db(self, meta_db_path: str, progress_callback=None) -> int:
        conn = sqlite3.connect(meta_db_path, check_same_thread=False)
        table = _resolve_occurrence_table(conn)
        rows = conn.execute(
            f"SELECT id,verbatimLocality FROM {table} WHERE (decimalLatitude IS NULL OR decimalLongitude IS NULL) AND verbatimLocality IS NOT NULL AND verbatimLocality != '' AND validationStatus != 'rejected'"
        ).fetchall()
        if not rows:
            conn.close(); return 0

        updated = 0
        for i,(row_id,vl) in enumerate(rows):
            occ = self.geocode_single({"verbatimLocality":vl,"decimalLatitude":None,"decimalLongitude":None})
            lat, lon = _to_float(occ.get("decimalLatitude")), _to_float(occ.get("decimalLongitude"))
            if lat is not None and lon is not None:
                conn.execute(
                    f"UPDATE {table} SET decimalLatitude=?,decimalLongitude=?,geocodingSource=? WHERE id=?",
                    (lat,lon,occ.get("geocodingSource",""),row_id))
                updated += 1
            if updated % 50 == 0 and updated > 0: conn.commit()
            if progress_callback: progress_callback(i+1,len(rows))
        conn.commit(); conn.close()
        return updated