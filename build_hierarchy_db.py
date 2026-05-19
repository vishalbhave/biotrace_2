# gpkg_path="biodiversity_data/destination_gpkg_folder/combined_layers.gpkg", output_db="biodiversity_data/locality_hierarchy.db"
    
    
import sqlite3
import fiona

def build_hierarchy_db(gpkg_path="biodiversity_data/destination_gpkg_folder/combined_layers.gpkg", output_db="biodiversity_data/locality_hierarchy.db"):
    print(f"Reading layers from {gpkg_path}...")
    layers = fiona.listlayers(gpkg_path)
    
    out_conn = sqlite3.connect(output_db)
    
    # Table 1: For the LLM Context builder
    out_conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_hierarchy (
            state TEXT, district TEXT, block TEXT, subdistrict TEXT,
            UNIQUE(state, district, block, subdistrict)
        )
    """)
    
    # Table 2: For the Geocoding Cascade
    out_conn.execute("""
        CREATE TABLE IF NOT EXISTS villages (
            village TEXT, district TEXT, state TEXT, layer_name TEXT
        )
    """)
    
    in_conn = sqlite3.connect(gpkg_path)
    total_villages = 0
    
    for layer in layers:
        print(f"Processing layer: {layer}...")
        try:
            # Detect village column name (varies by layer)
            cursor = in_conn.execute(f"PRAGMA table_info({layer})")
            cols = [c[1].lower() for c in cursor.fetchall()]
            v_col = next((c for c in ['village', 'vill_name', 'name'] if c in cols), None)
            
            if not v_col: continue
            
            # 1. Populate admin hierarchy (for _build_enriched_locality)
            query_admin = f"SELECT DISTINCT state_name, district, block, subdistric FROM {layer} WHERE state_name IS NOT NULL"
            out_conn.executemany("INSERT OR IGNORE INTO admin_hierarchy VALUES (?, ?, ?, ?)", in_conn.execute(query_admin).fetchall())
            
            # 2. Populate villages map (for geocoding cascade)
            query_villages = f"SELECT {v_col}, district, state_name, '{layer}' FROM {layer} WHERE {v_col} IS NOT NULL"
            villages_data = in_conn.execute(query_villages).fetchall()
            out_conn.executemany("INSERT INTO villages VALUES (?, ?, ?, ?)", villages_data)
            
            total_villages += len(villages_data)
        except Exception as e:
            print(f"Error processing {layer}: {e}")
            
    out_conn.commit()
    out_conn.execute("CREATE INDEX IF NOT EXISTS idx_vill_name ON villages(village)")
    out_conn.close()
    in_conn.close()
    
    print(f"Done! Extracted {total_villages} villages to {output_db}.")

if __name__ == "__main__":
    build_hierarchy_db()