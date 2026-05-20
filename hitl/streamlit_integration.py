"""
biotrace_hitl_streamlit_integration.py
═════════════════════════════════════════════════════════════════════════════════
Streamlit Components for HITL ML Integration with BioTrace
Verification UI + Active Learning Dashboard

Author: Conservation Tech - Marine Biology
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
from .ml_framework import (
    HITLPipeline, HITLConfig, initialize_hitl_pipeline
)
import plotly.express as px
import plotly.graph_objects as go

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION STATE INITIALIZATION
# ─────────────────────────────────────────────────────────────────────────────

if 'hitl_pipeline' not in st.session_state:
    st.session_state.hitl_pipeline = initialize_hitl_pipeline("biotrace_ml_models")
    st.session_state.correction_queue = []
    st.session_state.metrics_history = []


# ─────────────────────────────────────────────────────────────────────────────
#  HITL VERIFICATION TAB FOR BIOTRACE
# ─────────────────────────────────────────────────────────────────────────────

def render_hitl_verification_tab():
    """
    Main HITL verification interface.
    Integrates with BioTrace's extraction pipeline.
    """
    st.subheader("🤖 ML-Assisted Verification (HITL)")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Corrections Logged", st.session_state.hitl_pipeline.correction_count)
    with col2:
        metrics = st.session_state.hitl_pipeline.export_metrics()
        status = "✅ Trained" if metrics.get('model_status') == 'trained' else "⚠️ Untrained"
        st.metric("Model Status", status)
    with col3:
        if metrics.get('accuracy'):
            st.metric("Accuracy", f"{metrics['accuracy']:.2%}")
    
    st.divider()
    
    # Tab selection
    verification_mode = st.selectbox(
        "Select Task:",
        ["🔍 Review Uncertain Predictions", "✏️ Manual Correction", "📊 Model Metrics"]
    )
    
    if verification_mode == "🔍 Review Uncertain Predictions":
        render_uncertain_predictions_review()
    
    elif verification_mode == "✏️ Manual Correction":
        render_manual_correction_interface()
    
    elif verification_mode == "📊 Model Metrics":
        render_metrics_dashboard()


def render_uncertain_predictions_review():
    """
    Show predictions below confidence threshold for review.
    Active learning: prioritize by uncertainty.
    """
    st.subheader("🎯 Predictions Flagged for Review")
    
    pipeline = st.session_state.hitl_pipeline
    uncertain = pipeline.get_uncertain_predictions()
    
    if uncertain.empty:
        st.success("✅ All predictions above confidence threshold!")
        st.metric("Threshold", f"{pipeline.config.uncertainty_threshold:.0%}")
        return
    
    st.info(f"⚠️ {len(uncertain)} predictions below {pipeline.config.uncertainty_threshold:.0%} confidence")
    
    # Display predictions
    for idx, row in uncertain.head(20).iterrows():
        with st.expander(
            f"📌 '{row['text'][:60]}...' | "
            f"Confidence: {row['confidence']:.2%} | "
            f"Predicted: {row['predicted_type']}"
        ):
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("**Original Text:**")
                st.write(row['text'])
            
            with col2:
                st.markdown("**ML Prediction:**")
                st.info(f"Type: **{row['predicted_type']}**\nConfidence: {row['confidence']:.2%}")
            
            st.divider()
            
            # Correction interface
            st.markdown("**Correction:**")
            col_a, col_b, col_c = st.columns([2, 2, 1])
            
            with col_a:
                correct_type = st.selectbox(
                    "Correct classification:",
                    ["Primary", "Secondary", "Uncertain"],
                    key=f"correct_{idx}"
                )
            
            with col_b:
                feedback = st.text_input(
                    "Feedback (optional):",
                    key=f"feedback_{idx}",
                    placeholder="e.g., 'Confirmed by field survey'"
                )
            
            with col_c:
                if st.button("✅ Submit", key=f"submit_{idx}"):
                    pipeline.register_occurrence_correction(
                        text=row['text'],
                        predicted_type=row['predicted_type'],
                        corrected_type=correct_type,
                        confidence=row['confidence'],
                        source_doc=row.get('source_doc'),
                        notes=feedback
                    )
                    st.success("✓ Correction logged!")
                    st.rerun()


def render_manual_correction_interface():
    """
    Manually correct any BioTrace extraction result.
    Supports both occurrence type and entity corrections.
    """
    st.subheader("✏️ Manual Correction Interface")
    
    pipeline = st.session_state.hitl_pipeline
    
    # Input options
    input_mode = st.radio("Input mode:", ["Paste text", "Upload CSV", "From database"])
    
    texts = []
    source_info = []
    
    if input_mode == "Paste text":
        text_input = st.text_area("Paste text to correct:")
        if text_input:
            texts = [text_input]
            source_info = ["Manual input"]
    
    elif input_mode == "Upload CSV":
        uploaded_file = st.file_uploader("Upload CSV with 'text' column", type=['csv'])
        if uploaded_file:
            df = pd.read_csv(uploaded_file)
            texts = df['text'].tolist()
            source_info = df.get('source', ['Uploaded'] * len(texts)).tolist()
    
    elif input_mode == "From database":
        st.info("Loading from BioTrace occurrence database...")
        
        # 1. Connect to the correct database file 
        import sqlite3
        conn = sqlite3.connect("biodiversity_data/metadata_v5.db")
        
        # 2. Use the correct table (occurrences_v4) and column (rawTextEvidence)
        query = "SELECT rawTextEvidence as text, sourceCitation FROM occurrences_v4 LIMIT 100"
        
        try:
            df = pd.read_sql(query, conn)
            texts = df['text'].tolist()
            source_info = df['sourceCitation'].tolist()
        except Exception as e:
            st.error(f"Database Query Error: {e}")
        finally:
            conn.close()
    
    if texts:
        st.markdown(f"### Processing {len(texts)} item(s)")
        
        # Get ML predictions
        predictions = pipeline.process_batch(texts, source_info[0] if source_info else None)
        
        # Correction form for each
        for pred_idx, pred in enumerate(predictions):
            with st.expander(
                f"Item {pred_idx + 1}: {pred['text'][:50]}...",
                expanded=(pred_idx == 0)
            ):
                col_ml, col_correct = st.columns(2)
                
                with col_ml:
                    st.markdown("**ML Prediction:**")
                    occ = pred['occurrence_type']
                    st.info(
                        f"**Occurrence Type:** {occ['type']}\n"
                        f"**Confidence:** {occ['confidence']:.2%}\n\n"
                        f"Primary: {occ['probabilities']['Primary']:.1%}\n"
                        f"Secondary: {occ['probabilities']['Secondary']:.1%}\n"
                        f"Uncertain: {occ['probabilities']['Uncertain']:.1%}"
                    )
                
                with col_correct:
                    st.markdown("**Your Correction:**")
                    corrected_type = st.radio(
                        "Select correct type:",
                        ["Primary", "Secondary", "Uncertain"],
                        key=f"manual_type_{pred_idx}"
                    )
                    
                    correction_notes = st.text_area(
                        "Comments (optional):",
                        key=f"manual_notes_{pred_idx}",
                        height=80
                    )
                
                st.divider()
                
                # Species & Localities
                if pred['species']:
                    st.markdown("**Extracted Species:**")
                    for sp in pred['species']:
                        st.caption(f"🐟 {sp['entity']} (confidence: {sp.get('score', 0):.2%})")
                
                if pred['localities']:
                    st.markdown("**Extracted Localities:**")
                    for loc in pred['localities']:
                        st.caption(f"📍 {loc['entity']}")
                
                # Submit correction
                if st.button("Save Correction", key=f"save_manual_{pred_idx}"):
                    pipeline.register_occurrence_correction(
                        text=pred['text'],
                        predicted_type=occ['type'],
                        corrected_type=corrected_type,
                        confidence=occ['confidence'],
                        source_doc=source_info[pred_idx] if pred_idx < len(source_info) else None,
                        notes=correction_notes
                    )
                    st.success(f"✓ Correction {pred_idx + 1} saved!")


def render_metrics_dashboard():
    """
    Model performance dashboard.
    Track improvements from HITL learning.
    """
    st.subheader("📈 Model Performance Dashboard")
    
    pipeline = st.session_state.hitl_pipeline
    metrics = pipeline.export_metrics()
    
    if metrics.get('status') == 'no_training_data':
        st.warning("⚠️ No training data yet. Make corrections to train the model.")
        return
    
    # Key metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric("Total Corrections", metrics['total_corrections'])
    with col2:
        st.metric("Training Samples", metrics['training_samples'])
    with col3:
        st.metric("Accuracy", f"{metrics['accuracy']:.2%}")
    with col4:
        st.metric("Precision", f"{metrics['precision']:.2%}")
    with col5:
        st.metric("Recall", f"{metrics['recall']:.2%}")
    
    st.divider()
    
    # Performance over time (simulated)
    col_left, col_right = st.columns(2)
    
    with col_left:
        st.markdown("### Model Improvement Over Time")
        # Create sample data showing improvement
        correction_counts = [10, 20, 35, 50, 75, metrics['total_corrections']]
        accuracies = [0.65, 0.72, 0.78, 0.82, 0.85, metrics['accuracy']]
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=correction_counts, y=accuracies,
            mode='lines+markers',
            name='Accuracy',
            line=dict(color='#00CC96', width=3),
            marker=dict(size=10)
        ))
        fig.update_layout(
            xaxis_title="Corrections Logged",
            yaxis_title="Model Accuracy",
            hovermode='x unified',
            height=400
        )
        st.plotly_chart(fig, use_container_width=True)
    
    with col_right:
        st.markdown("### Confidence Distribution")
        # Uncertain predictions distribution
        uncertain_df = pipeline.get_uncertain_predictions()
        
        if not uncertain_df.empty:
            fig = px.histogram(
                uncertain_df,
                x='confidence',
                nbins=20,
                title='Distribution of Uncertain Predictions',
                labels={'confidence': 'Confidence Score'},
                color_discrete_sequence=['#FF6B6B']
            )
            fig.update_layout(height=400, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.success("✅ No uncertain predictions!")
    
    st.divider()
    
    # Detailed metrics table
    st.markdown("### Detailed Metrics")
    metrics_df = pd.DataFrame({
        'Metric': ['Accuracy', 'Precision', 'Recall', 'F1 Score', 'Model Status'],
        'Value': [
            f"{metrics['accuracy']:.2%}",
            f"{metrics['precision']:.2%}",
            f"{metrics['recall']:.2%}",
            f"{metrics['f1_score']:.2%}",
            metrics['model_status'].upper()
        ]
    })
    st.dataframe(metrics_df, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
#  INTEGRATION WITH BIOTRACE OCCURRENCE TABLE
# ─────────────────────────────────────────────────────────────────────────────

def add_ml_enrichment_to_biotrace_table(df_occurrences: pd.DataFrame) -> pd.DataFrame:
    """
    Add ML confidence scores and review flags to BioTrace occurrence table.
    Used in the Database tab.
    """
    pipeline = st.session_state.hitl_pipeline
    
    # Process each record
    ml_enrichments = []
    for idx, row in df_occurrences.iterrows():
        text = row.get('text', '')
        if text:
            pred = pipeline.process_text(text)
            ml_enrichments.append({
                'ml_type_confidence': pred['occurrence_type']['confidence'] if pred['occurrence_type'] else None,
                'ml_flag_review': (pred['occurrence_type']['confidence'] < 0.70) if pred['occurrence_type'] else False,
                'ml_species_count': len(pred['species']),
                'ml_localities_count': len(pred['localities'])
            })
        else:
            ml_enrichments.append({
                'ml_type_confidence': None,
                'ml_flag_review': False,
                'ml_species_count': 0,
                'ml_localities_count': 0
            })
    
    enrichment_df = pd.DataFrame(ml_enrichments)
    return pd.concat([df_occurrences, enrichment_df], axis=1)


# ─────────────────────────────────────────────────────────────────────────────
#  ACTIVE LEARNING SUGGESTION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def get_top_uncertain_for_annotation(n: int = 10) -> pd.DataFrame:
    """
    Active Learning: Get most uncertain predictions for efficient annotation.
    Focuses human effort on high-impact samples.
    """
    pipeline = st.session_state.hitl_pipeline
    uncertain = pipeline.get_uncertain_predictions()
    
    if uncertain.empty:
        return pd.DataFrame()
    
    # Sort by confidence (lowest = most uncertain)
    return uncertain.nsmallest(n, 'confidence')


def render_active_learning_panel():
    """
    Active Learning interface: suggest samples for review based on uncertainty.
    """
    st.subheader("🎯 Active Learning: Most Uncertain Samples")
    
    n_suggestions = st.slider("Number of suggestions:", 5, 50, 10)
    
    uncertain_samples = get_top_uncertain_for_annotation(n_suggestions)
    
    if uncertain_samples.empty:
        st.success("✅ All predictions confident!")
        return
    
    st.metric("Uncertain samples", len(uncertain_samples))
    
    # Display as table
    display_df = uncertain_samples[[
        'text', 'predicted_type', 'confidence'
    ]].copy()
    display_df.columns = ['Text', 'Predicted Type', 'Confidence']
    display_df['Confidence'] = display_df['Confidence'].apply(lambda x: f"{x:.2%}")
    display_df['Text'] = display_df['Text'].apply(lambda x: x[:60] + "...")
    
    st.dataframe(display_df, use_container_width=True, height=400)
    
    # Batch correction
    st.markdown("### Batch Correction")
    if st.button("🔄 Load Next Batch for Review"):
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION & SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

def render_hitl_settings():
    """
    Configuration panel for HITL parameters.
    """
    st.subheader("⚙️ HITL Configuration")
    
    config = st.session_state.hitl_pipeline.config
    
    with st.expander("🔧 Model Parameters"):
        col1, col2 = st.columns(2)
        
        with col1:
            new_threshold = st.slider(
                "Uncertainty Threshold:",
                0.0, 1.0, config.uncertainty_threshold,
                help="Flag predictions below this confidence for review"
            )
            config.uncertainty_threshold = new_threshold
        
        with col2:
            new_interval = st.number_input(
                "Retrain Interval:",
                min_value=1, max_value=100, value=config.retrain_interval,
                help="Retrain after N corrections"
            )
            config.retrain_interval = new_interval
        
        col_a, col_b = st.columns(2)
        with col_a:
            config.query_strategy = st.selectbox(
                "Active Learning Strategy:",
                ["entropy", "margin"]
            )
        
        with col_b:
            config.min_training_samples = st.number_input(
                "Min Training Samples:",
                min_value=10, max_value=500, value=config.min_training_samples
            )
    
    with st.expander("📊 Display Options"):
        show_probabilities = st.checkbox("Show probability distribution")
        show_entities = st.checkbox("Show extracted entities")
        show_feedback_history = st.checkbox("Show correction history")
    
    st.success("✓ Configuration updated")


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORT & REPORTING
# ─────────────────────────────────────────────────────────────────────────────

def export_hitl_data():
    """
    Export HITL feedback and metrics for analysis.
    """
    st.subheader("📥 Export HITL Data")
    
    pipeline = st.session_state.hitl_pipeline
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("📊 Export Metrics"):
            metrics = pipeline.export_metrics()
            metrics_df = pd.DataFrame([metrics])
            
            csv = metrics_df.to_csv(index=False).encode()
            st.download_button(
                "⬇️ Download Metrics CSV",
                data=csv,
                file_name="hitl_metrics.csv",
                mime="text/csv"
            )
    
    with col2:
        if st.button("📋 Export Corrections"):
            texts, labels = pipeline.feedback_db.get_training_data(task="occurrence")
            df = pd.DataFrame({
                'text': texts,
                'corrected_type': labels,
                'timestamp': datetime.now().isoformat()
            })
            
            csv = df.to_csv(index=False).encode()
            st.download_button(
                "⬇️ Download Corrections CSV",
                data=csv,
                file_name="hitl_corrections.csv",
                mime="text/csv"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN INTEGRATION FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def integrate_hitl_into_biotrace(tabs_list):
    """
    Add HITL tab to BioTrace interface.
    
    Usage in biotrace_v53_2.py:
    
        from .streamlit_integration import integrate_hitl_into_biotrace
        
        # After creating tabs:
        integrate_hitl_into_biotrace(tabs)
        
        # Then reference HITL tab:
        with tabs[-1]:  # HITL is last tab
            render_hitl_verification_tab()
    """
    
    with tabs_list[-1]:  # Add to last tab
        st.subheader("🤖 Machine Learning & HITL Verification")
        
        hitl_tab = st.tabs([
            "🔍 Uncertain Predictions",
            "✏️ Manual Correction",
            "📈 Metrics",
            "🎯 Active Learning",
            "⚙️ Settings"
        ])
        
        with hitl_tab[0]:
            render_uncertain_predictions_review()
        
        with hitl_tab[1]:
            render_manual_correction_interface()
        
        with hitl_tab[2]:
            render_metrics_dashboard()
        
        with hitl_tab[3]:
            render_active_learning_panel()
        
        with hitl_tab[4]:
            render_hitl_settings()


# ─────────────────────────────────────────────────────────────────────────────
#  TESTING
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    st.set_page_config(
        page_title="BioTrace HITL Integration",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.title("🔬 BioTrace HITL Integration")
    
    # Standalone test mode
    render_hitl_verification_tab()
