#!/bin/bash
set -e

# 1. Folders & Moving
mkdir -p extractors chunkers hitl utils wiki ui knowledge_graph tests patches docs/images archive

# Move files
mv biotrace_ner.py extractors/ner.py
mv biotrace_hf_ner.py extractors/hf_ner.py
mv biotrace_locality_ner.py extractors/locality_ner.py
mv biotrace_morpho_extractor.py extractors/morpho_extractor.py
mv biotrace_relation_extractor.py extractors/relation_extractor.py
mv biotrace_author_extractor.py extractors/author_extractor.py
mv biotrace_biocenteric_extractor.py extractors/biocenteric_extractor.py
mv biotrace_gna_inverted.py extractors/gna_inverted.py
mv biotrace_gnv.py extractors/gnv.py

mv biotrace_chunker.py chunkers/chunker.py
mv biotrace_agentic_chunker.py chunkers/agentic_chunker.py
mv biotrace_hierarchical_chunker.py chunkers/hierarchical_chunker.py
mv biotrace_scientific_chunker.py chunkers/scientific_chunker.py
mv biotrace_table_chunker_patch.py archive/table_chunker_patch.py

mv biotrace_hitl_geocoding.py hitl/geocoding.py
mv biotrace_hitl_ml_framework.py hitl/ml_framework.py
mv biotrace_hitl_streamlit_integration.py hitl/streamlit_integration.py
mv biotrace_hitl_implementation_guide.py hitl/implementation_guide.py
mv biotrace_hitl_requirements.txt hitl/requirements.txt

mv biotrace_col_client.py utils/col_client.py
mv biotrace_dedup_patch.py archive/dedup_patch.py
mv biotrace_gbif_verifier.py utils/gbif_verifier.py
mv biotrace_ocr.py utils/ocr.py
mv biotrace_pdf_meta.py utils/pdf_meta.py
mv biotrace_schema.py utils/schema.py
mv biotrace_taxonomy.py utils/taxonomy.py
mv biotrace_taxon_filter.py utils/taxon_filter.py
mv biotrace_progress_logger.py utils/progress_logger.py
mv biotrace_traiter_prepass.py utils/traiter_prepass.py
mv biotrace_parallel_engine.py utils/parallel_engine.py
mv biotrace_geocoding_lifestage_patch.py archive/geocoding_lifestage_patch.py
mv biotrace_locality_guard_patch.py archive/locality_guard_patch.py
mv biotrace_phase0_dwca_bootstrap.py utils/phase0_dwca_bootstrap.py
mv biotrace_phase1_geocoding.py utils/phase1_geocoding.py
mv biotrace_unified_verifier.py utils/unified_verifier.py
mv biotrace_agent_loop.py utils/agent_loop.py
mv biotrace_postprocessing.py utils/postprocessing.py
mv biotrace_table_preprocessor.py utils/table_preprocessor.py

mv biotrace_unified_wiki.py wiki/unified_wiki.py
mv biotrace_docling_wiki_bridge.py wiki/docling_bridge.py
mv biotrace_docling_bridge_v56_patch.py archive/docling_bridge_v56_patch.py
mv biotrace_scientific_wiki_engine.py wiki/scientific_engine.py
mv biotrace_wiki_agent.py wiki/agent.py
mv biotrace_wiki_agent_v56.py wiki/agent_v56.py
mv biotrace_wiki_autopop.py wiki/autopop.py
mv biotrace_wiki_enhanced.py wiki/enhanced.py
mv biotrace_wiki_v56.py wiki/v56.py
mv biotrace_wiki_v56_patch.py archive/v56_patch.py
mv biotrace_wiki.css wiki/wiki.css

mv biotrace_kg_spatio_temporal.py knowledge_graph/spatio_temporal.py
mv biotrace_knowledge_graph.py knowledge_graph/knowledge_graph.py
mv biotrace_memory_bank.py knowledge_graph/memory_bank.py
mv biotrace_md_cache.py knowledge_graph/md_cache.py

mv biotrace_v53_2.py patches/v53_2.py
mv biotrace_v53_2_1_parallel_patch.py patches/v53_2_1_parallel_patch.py
mv biotrace_v53_integration_patch.py patches/v53_integration_patch.py
mv biotrace_v56_integration.py patches/v56_integration.py
mv biotrace_patch57_update.py patches/patch57_update.py
mv biotrace_v5_enhancements.py patches/v5_enhancements.py

# We moved the main file to archive recently, let's restore it and rename it.
if [ -f "archive/biotrace_v53_2_1.py" ]; then
    mv archive/biotrace_v53_2_1.py main.py
else
    # Maybe it's still here if the agent restored to the original state
    mv biotrace_v53_2_1.py main.py
fi

mv biotrace_llm_orchestration_strategy.md docs/llm_orchestration_strategy.md 2>/dev/null || true

touch extractors/__init__.py chunkers/__init__.py hitl/__init__.py utils/__init__.py wiki/__init__.py knowledge_graph/__init__.py patches/__init__.py
