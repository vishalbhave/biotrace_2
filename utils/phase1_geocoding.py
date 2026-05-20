"""
biotrace_phase1_village_geocoding.py

PHASE 1: India Village GPKG → Geocoding Cascade + Manual Override
- GPKG native spatial index (no memory conversion)
- Point-in-polygon validation + confidence scoring
- Fallback: nearest village suggestions
- Manual override UI with map picker
- Corrections logged to feedback DB for locality_ner retraining
"""

import sqlite3
import geopandas as gpd
from shapely.geometry import Point
from pathlib import Path
from typing import Dict, Optional, List
import logging
from datetime import datetime

import streamlit as st
import folium
from streamlit_folium import st_folium
import pandas as pd
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)


class GPKGVillageGeocoder:
    """GPKG native spatial indexing via geopandas"""
    
    def __init__(self, gpkg_path: str, layer_name: str = None):
        self.gpkg_path = Path(gpkg_path)
        if not self.gpkg_path.exists():
            raise FileNotFoundError(f"GPKG not found: {self.gpkg_path}")
        
        # Auto-detect layer
        layers = gpd.io.file.fiona.listlayers(str(self.gpkg_path))
        layer_name = layer_name or layers[0]
        
        logger.info(f"Loading {layer_name} from {self.gpkg_path.name}")
        self.gdf = gpd.read_file(self.gpkg_path, layer=layer_name)
        
        if self.gdf.crs != 'EPSG:4326':
            self.gdf = self.gdf.to_crs('EPSG:4326')
        
        self.spatial_index = self.gdf.sindex
        self.col_names = self._detect_columns()
        logger.info(f"✓ Loaded {len(self.gdf)} villages")
    
    def _detect_columns(self) -> Dict[str, str]:
        """Auto-detect standard column names"""
        cols_lower = {col.lower(): col for col in self.gdf.columns}
        names = {}
        
        for std, candidates in {
            'village': ['name', 'village_name', 'gn_name', 'vill_name'],
            'district': ['district', 'district_name'],
            'state': ['state', 'state_name'],
            'taluka': ['taluk', 'taluka', 'block'],
        }.items():
            for cand in candidates:
                if cand in cols_lower:
                    names[std] = cols_lower[cand]
                    break
        
        return names
    
    def reverse_geocode(self, lat: float, lon: float) -> Optional[Dict]:
        """Point-in-polygon: village containing point"""
        point = Point(lon, lat)
        candidates = list(self.spatial_index.intersection(point.bounds))
        
        for idx in candidates:
            if self.gdf.iloc[idx].geometry.contains(point):
                village = self.gdf.iloc[idx]
                return {
                    'village': village.get(self.col_names.get('village')),
                    'district': village.get(self.col_names.get('district')),
                    'state': village.get(self.col_names.get('state')),
                    'source': 'polygon_match',
                    'confidence': 0.98,
                }
        return None
    
    def nearest_villages(self, lat: float, lon: float, k: int = 5, max_dist_km: float = 10) -> List[Dict]:
        """K nearest villages within radius"""
        point = Point(lon, lat)
        gdf_proj = self.gdf.to_crs('EPSG:3857')
        point_proj = Point(point).buffer(max_dist_km * 1000)
        
        nearby = gdf_proj[gdf_proj.geometry.intersects(point_proj)]
        if nearby.empty:
            return []
        
        distances = nearby.geometry.distance(point_proj.centroid)
        nearest_idx = distances.nsmallest(k).index
        
        results = []
        for idx in nearest_idx:
            v = self.gdf.loc[idx]
            dist_m = distances[idx]
            results.append({
                'village': v.get(self.col_names.get('village')),
                'district': v.get(self.col_names.get('district')),
                'state': v.get(self.col_names.get('state')),
                'distance_km': dist_m / 1000,
                'confidence': max(0.5, 0.95 - (dist_m / 1000 / max_dist_km) * 0.4),
                'latitude': float(v.geometry.centroid.y),
                'longitude': float(v.geometry.centroid.x),
            })
        return results


class EnhancedGeocodingCascade:
    """Nominatim → Polygon Validation → Nearest Village → Manual"""
    
    def __init__(self, gpkg_path: str, feedback_db_path: str = None):
        self.geocoder = GPKGVillageGeocoder(gpkg_path)
        self.nominatim = Nominatim(user_agent='biotrace_v5', timeout=10)
        self.feedback_db_path = Path(feedback_db_path) if feedback_db_path else None
    
    def geocode(self, locality_name: str, state: str = None) -> Dict:
        """Cascade: Nominatim → Polygon → Nearest → Flag"""
        result = {
            'locality': locality_name,
            'success': False,
            'confidence': 0.0,
            'latitude': None,
            'longitude': None,
            'flag_for_manual': False,
            'alternatives': [],
        }
        
        try:
            loc = self.nominatim.geocode(f"{locality_name}, {state or 'India'}")
            if loc:
                lat, lon = loc.latitude, loc.longitude
                result['latitude'], result['longitude'] = lat, lon
                
                # Validate against polygon
                poly = self.geocoder.reverse_geocode(lat, lon)
                if poly:
                    result['success'] = True
                    result['confidence'] = 0.95
                    result['polygon_matched'] = poly
                else:
                    # Suggest nearest
                    nearest = self.geocoder.nearest_villages(lat, lon, k=3)
                    result['alternatives'] = nearest
                    result['confidence'] = 0.65 if nearest else 0.5
                    result['flag_for_manual'] = True
        except:
            result['flag_for_manual'] = True
        
        return result
    
    def log_correction(self, locality_text: str, selected: Dict):
        """Log to ner_corrections table for locality_ner retraining"""
        if not self.feedback_db_path:
            return
        
        with sqlite3.connect(self.feedback_db_path) as conn:
            conn.execute("""
                INSERT INTO ner_corrections
                (text, entity_type, predicted_entity, corrected_entity,
                 confidence, timestamp, source_doc, user_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                locality_text, 'locality', 'uncertain',
                f"{selected.get('village', '')},{selected.get('district', '')}",
                0.95, datetime.now().isoformat(), 'manual_geocoding',
                f"lat:{selected.get('latitude')},lon:{selected.get('longitude')}"
            ))
            conn.commit()


def render_geocoding_widget(occurrence: Dict, cascade: EnhancedGeocodingCascade) -> Optional[Dict]:
    """Manual geocoding UI"""
    st.markdown(f"### 📍 {occurrence.get('locality')}")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        alts = occurrence.get('alternatives', [])
        if alts:
            idx = st.radio(
                "Nearby villages:",
                range(len(alts)),
                format_func=lambda i: f"{alts[i]['village']}, {alts[i]['district']} ({alts[i]['distance_km']:.1f}km)"
            )
            selected = alts[idx]
            
            m = folium.Map(location=[selected['latitude'], selected['longitude']], zoom_start=12)
            folium.Marker([selected['latitude'], selected['longitude']], popup=selected['village']).add_to(m)
            st_folium(m, width=500, height=400)
            
            if st.button("✅ Confirm"):
                cascade.log_correction(occurrence.get('locality'), selected)
                return selected
    
    with col2:
        st.markdown("**Manual entry:**")
        lat = st.number_input("Lat", value=0.0, step=0.01, format="%.4f")
        lon = st.number_input("Lon", value=0.0, step=0.01, format="%.4f")
        if st.button("✅ Confirm manual"):
            sel = {'village': 'Manual', 'latitude': lat, 'longitude': lon}
            cascade.log_correction(occurrence.get('locality'), sel)
            return sel
    
    return None


def integrate_geocoding(occurrences: List[Dict], cascade: EnhancedGeocodingCascade):
    """Verify and override geocoding in verification table"""
    st.subheader("📍 Geocoding")
    
    results = []
    for occ in occurrences:
        if not occ.get('latitude'):
            res = cascade.geocode(occ.get('locality'), occ.get('state'))
            
            if res.get('flag_for_manual'):
                st.warning(f"⚠️ {occ.get('locality')} needs manual override")
                corrected = render_geocoding_widget(res, cascade)
                if corrected:
                    occ['latitude'], occ['longitude'] = corrected['latitude'], corrected['longitude']
                    results.append({'locality': occ['locality'], 'lat': corrected['latitude'], 'lon': corrected['longitude'], 'status': '✅'})
            else:
                results.append({'locality': occ['locality'], 'lat': res['latitude'], 'lon': res['longitude'], 'status': '✅'})
    
    if results:
        st.dataframe(pd.DataFrame(results), use_container_width=True)
