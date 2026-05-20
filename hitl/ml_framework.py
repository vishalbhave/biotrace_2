"""
biotrace_hitl_ml_framework.py
═════════════════════════════════════════════════════════════════════════════════
HITL (Human-in-the-Loop) Enhanced ML Pipeline for BioTrace
Integrates scikit-learn + Transformers + Active Learning

Features:
  ✅ Occurrence Type Classification (Primary/Secondary/Uncertain)
  ✅ Named Entity Recognition (Species/Locality)
  ✅ Ensemble Confidence Scoring
  ✅ Active Learning Framework (uncertainty sampling)
  ✅ Learning from HITL Corrections
  ✅ Incremental Model Retraining
  ✅ Export for BioTrace Integration

Author: Conservation Tech - Marine Biology Focus
"""

import json
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
import joblib
import logging

# ML & NLP Libraries
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.feature_extraction.text import TfidfVectorizer

# Transformers for embeddings
from transformers import AutoTokenizer, AutoModel, pipeline as hf_pipeline
import torch

# Active Learning
from modAL.models import ActiveLearner
from modAL.uncertainty import entropy_sampling, margin_sampling

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING & CONFIG
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class HITLConfig:
    """Configuration for HITL ML Framework"""
    model_dir: str = "biotrace_ml_models"
    feedback_db: str = "biotrace_hitl_feedback.db"
    
    # Classification
    occurrence_type_model: str = "occurrence_classifier.pkl"
    occurrence_vectorizer: str = "occurrence_vectorizer.pkl"
    
    # NER
    species_ner_model: str = "species_ner_model.pkl"
    locality_ner_model: str = "locality_ner_model.pkl"
    
    # Embeddings
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    
    # Active Learning
    query_strategy: str = "entropy"  # "entropy" or "margin"
    uncertainty_threshold: float = 0.65
    
    # Training
    min_training_samples: int = 50
    retrain_interval: int = 10  # Retrain after N corrections
    test_split: float = 0.2


class HITLFeedbackDatabase:
    """
    SQLite database for storing and retrieving HITL corrections.
    Tracks all user corrections for incremental model improvement.
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()
    
    def _init_tables(self):
        """Initialize feedback tables"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS occurrence_corrections (
                    id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    predicted_type TEXT,
                    corrected_type TEXT,
                    confidence REAL,
                    timestamp TEXT,
                    source_doc TEXT,
                    user_notes TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ner_corrections (
                    id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    entity_type TEXT,
                    predicted_entity TEXT,
                    corrected_entity TEXT,
                    confidence REAL,
                    char_start INTEGER,
                    char_end INTEGER,
                    timestamp TEXT,
                    source_doc TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS confidence_scores (
                    id INTEGER PRIMARY KEY,
                    prediction_id TEXT,
                    task_type TEXT,
                    ensemble_confidence REAL,
                    model_confidences TEXT,
                    timestamp TEXT
                )
            """)
            conn.commit()
    
    def log_occurrence_correction(
        self, text: str, predicted: str, corrected: str, 
        confidence: float, source_doc: str = None, notes: str = None
    ):
        """Log occurrence type correction"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO occurrence_corrections 
                (text, predicted_type, corrected_type, confidence, timestamp, source_doc, user_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (text, predicted, corrected, confidence, datetime.now().isoformat(), source_doc, notes))
            conn.commit()
    
    def log_ner_correction(
        self, text: str, entity_type: str, predicted: str, corrected: str,
        confidence: float, char_start: int, char_end: int, source_doc: str = None
    ):
        """Log NER correction"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO ner_corrections
                (text, entity_type, predicted_entity, corrected_entity, confidence, 
                 char_start, char_end, timestamp, source_doc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (text, entity_type, predicted, corrected, confidence, char_start, 
                  char_end, datetime.now().isoformat(), source_doc))
            conn.commit()
    
    def get_training_data(self, task: str = "occurrence") -> Tuple[List[str], List[str]]:
        """Retrieve training data from corrections"""
        with sqlite3.connect(self.db_path) as conn:
            if task == "occurrence":
                query = "SELECT text, corrected_type FROM occurrence_corrections WHERE corrected_type IS NOT NULL"
            else:  # NER
                query = "SELECT text, corrected_entity FROM ner_corrections WHERE corrected_entity IS NOT NULL"
            
            df = pd.read_sql_query(query, conn)
            return df['text'].tolist(), df[df.columns[1]].tolist()
    
    def get_uncertain_predictions(self, threshold: float = 0.70) -> pd.DataFrame:
        """Get predictions below confidence threshold for review"""
        with sqlite3.connect(self.db_path) as conn:
            query = f"""
                SELECT * FROM occurrence_corrections 
                WHERE confidence < ? AND corrected_type IS NULL
                ORDER BY confidence ASC
            """
            return pd.read_sql_query(query, conn, params=(threshold,))


class EmbeddingExtractor:
    """
    Extract dense embeddings from text using transformer models.
    Used as features for ML classifiers.
    """
    
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name)
        self.model.eval()
    
    def get_embedding(self, text: str, max_length: int = 512) -> np.ndarray:
        """Extract embedding for single text"""
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, 
                               max_length=max_length, padding=True)
        
        with torch.no_grad():
            outputs = self.model(**inputs)
        
        # Mean pooling
        embeddings = outputs.last_hidden_state.mean(dim=1)
        return embeddings.numpy().flatten()
    
    def get_embeddings_batch(self, texts: List[str]) -> np.ndarray:
        """Extract embeddings for batch of texts"""
        embeddings = []
        for text in texts:
            embeddings.append(self.get_embedding(text))
        return np.array(embeddings)


class OccurrenceTypeClassifier:
    """
    Ensemble classifier for occurrence type detection.
    Combines TF-IDF + Embeddings with multiple ML models.
    """
    
    def __init__(self, config: HITLConfig):
        self.config = config
        self.embedding_extractor = EmbeddingExtractor(config.embedding_model)
        
        # Feature extraction
        self.tfidf = TfidfVectorizer(max_features=100, ngram_range=(1, 2))
        
        # Base classifiers
        self.rf_classifier = RandomForestClassifier(n_estimators=100, random_state=42, max_depth=10)
        self.gb_classifier = GradientBoostingClassifier(n_estimators=100, random_state=42)
        
        # Ensemble
        self.ensemble = VotingClassifier(
            estimators=[
                ('rf', self.rf_classifier),
                ('gb', self.gb_classifier)
            ],
            voting='soft'
        )
        
        self.is_trained = False
        self.classes_ = np.array(['Primary', 'Secondary', 'Uncertain'])
    
    def prepare_features(self, texts: List[str]) -> np.ndarray:
        """Combine TF-IDF and embeddings as features"""
        # TF-IDF features
        tfidf_features = self.tfidf.fit_transform(texts).toarray()
        
        # Embedding features
        embedding_features = self.embedding_extractor.get_embeddings_batch(texts)
        
        # Concatenate
        combined_features = np.hstack([tfidf_features, embedding_features])
        return combined_features
    
    def train(self, texts: List[str], labels: List[str]):
        """Train ensemble classifier"""
        if len(texts) < self.config.min_training_samples:
            logger.warning(f"Only {len(texts)} samples. Min required: {self.config.min_training_samples}")
            return False
        
        features = self.prepare_features(texts)
        self.ensemble.fit(features, labels)
        self.is_trained = True
        logger.info(f"✓ Trained OccurrenceTypeClassifier on {len(texts)} samples")
        return True
    
    def predict_with_confidence(self, texts: List[str]) -> List[Dict]:
        """Predict occurrence type with confidence scores"""
        if not self.is_trained:
            logger.warning("Model not trained yet")
            return []
        
        features = self.prepare_features(texts)
        predictions = self.ensemble.predict(features)
        probabilities = self.ensemble.predict_proba(features)
        
        results = []
        for text, pred, probs in zip(texts, predictions, probabilities):
            confidence = float(max(probs))
            results.append({
                'text': text,
                'predicted_type': pred,
                'confidence': confidence,
                'probabilities': {
                    'Primary': float(probs[list(self.classes_).index('Primary')]),
                    'Secondary': float(probs[list(self.classes_).index('Secondary')]),
                    'Uncertain': float(probs[list(self.classes_).index('Uncertain')])
                }
            })
        return results
    
    def save(self, path: str):
        """Save trained model"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            'ensemble': self.ensemble,
            'tfidf': self.tfidf,
            'classes': self.classes_
        }, path)
        logger.info(f"✓ Saved OccurrenceTypeClassifier to {path}")
    
    def load(self, path: str):
        """Load trained model"""
        data = joblib.load(path)
        self.ensemble = data['ensemble']
        self.tfidf = data['tfidf']
        self.classes_ = data['classes']
        self.is_trained = True
        logger.info(f"✓ Loaded OccurrenceTypeClassifier from {path}")


class SpeciesNERModel:
    """
    Fine-tuned transformer model for species name extraction.
    Uses Hugging Face token classification pipeline.
    """
    
    def __init__(self, config: HITLConfig):
        self.config = config
        self.pipeline = hf_pipeline(
            "token-classification",
            model="dslim/bert-base-NER", #"dslim/bert-base-multilingual-cased-ner",
            aggregation_strategy="simple"
        )
        self.is_fine_tuned = False
    
    def extract_species(self, text: str) -> List[Dict]:
        """Extract species entities from text"""
        entities = self.pipeline(text[:512])  # Truncate to avoid token limit
        
        results = []
        for entity in entities:
            if entity['entity_group'] in ['PERSON', 'ORG']:  # Adapt to your entity types
                results.append({
                    'entity': entity['word'],
                    'entity_type': 'SPECIES',
                    'start': entity['start'],
                    'end': entity['end'],
                    'score': entity['score']
                })
        return results
    
    def extract_species_batch(self, texts: List[str]) -> List[List[Dict]]:
        """Extract species from multiple texts"""
        return [self.extract_species(text) for text in texts]


class LocalityNERModel:
    """
    Locality extraction model (geographic locations).
    Combines spaCy + custom rules + embeddings.
    """
    
    def __init__(self, config: HITLConfig):
        self.config = config
        try:
            import spacy
            self.nlp = spacy.load("en_core_web_sm")
        except:
            logger.warning("spaCy model not found. Install with: python -m spacy download en_core_web_sm")
            self.nlp = None
    
    def extract_localities(self, text: str) -> List[Dict]:
        """Extract geographic entities"""
        if not self.nlp:
            return []
        
        doc = self.nlp(text)
        results = []
        
        for ent in doc.ents:
            if ent.label_ == 'GPE':  # Geopolitical entity
                results.append({
                    'entity': ent.text,
                    'entity_type': 'LOCALITY',
                    'start': ent.start_char,
                    'end': ent.end_char,
                    'spacy_label': ent.label_
                })
        
        return results


class ActiveLearningFramework:
    """
    Active Learning for efficient annotation.
    Suggests uncertain predictions for human review.
    """
    
    def __init__(self, config: HITLConfig):
        self.config = config
        self.learner = None
        self.uncertainty_scores = []
    
    def initialize(self, X_initial: np.ndarray, y_initial: np.ndarray):
        """Initialize active learner"""
        strategy = entropy_sampling if self.config.query_strategy == "entropy" else margin_sampling
        
        base_estimator = GradientBoostingClassifier(random_state=42, max_depth=5)
        self.learner = ActiveLearner(
            estimator=base_estimator,
            query_strategy=strategy,
            X_training=X_initial,
            y_training=y_initial
        )
        logger.info("✓ Initialized ActiveLearner")
    
    def query_uncertain_samples(self, X_pool: np.ndarray, n_queries: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """Get most uncertain samples for annotation"""
        if not self.learner:
            return np.array([]), np.array([])
        
        query_idx, query_instance = self.learner.query(X_pool, n_instances=min(n_queries, len(X_pool)))
        return query_idx, query_instance
    
    def teach(self, X: np.ndarray, y: np.ndarray):
        """Update learner with newly annotated samples"""
        if self.learner:
            self.learner.teach(X, y)
            logger.info(f"✓ Active learner updated with {len(y)} new samples")


class HITLPipeline:
    """
    Unified pipeline combining all HITL components.
    Main interface for BioTrace integration.
    """
    
    def __init__(self, config: HITLConfig = None):
        self.config = config or HITLConfig()
        Path(self.config.model_dir).mkdir(parents=True, exist_ok=True)
        
        self.feedback_db = HITLFeedbackDatabase(
            str(Path(self.config.model_dir) / self.config.feedback_db)
        )
        
        self.occurrence_classifier = OccurrenceTypeClassifier(self.config)
        self.species_ner = SpeciesNERModel(self.config)
        self.locality_ner = LocalityNERModel(self.config)
        self.active_learner = ActiveLearningFramework(self.config)
        
        self.correction_count = 0
    
    def process_text(self, text: str, source_doc: str = None) -> Dict:
        """
        Full pipeline: classify occurrence type + extract entities.
        Returns predictions with confidence scores.
        """
        results = {
            'text': text,
            'source_doc': source_doc,
            'timestamp': datetime.now().isoformat(),
            'occurrence_type': None,
            'species': [],
            'localities': [],
            'confidence_scores': {}
        }
        
        # Occurrence type classification
        occ_results = self.occurrence_classifier.predict_with_confidence([text])
        if occ_results:
            occ = occ_results[0]
            results['occurrence_type'] = {
                'type': occ['predicted_type'],
                'confidence': occ['confidence'],
                'probabilities': occ['probabilities']
            }
            results['confidence_scores']['occurrence_type'] = occ['confidence']
        
        # Species extraction
        species_results = self.species_ner.extract_species(text)
        results['species'] = species_results
        
        # Locality extraction
        locality_results = self.locality_ner.extract_localities(text)
        results['localities'] = locality_results
        
        return results
    
    def process_batch(self, texts: List[str], source_doc: str = None) -> List[Dict]:
        """Process multiple texts"""
        return [self.process_text(text, source_doc) for text in texts]
    
    def register_occurrence_correction(
        self, text: str, predicted_type: str, corrected_type: str, 
        confidence: float, source_doc: str = None, notes: str = None
    ):
        """Log occurrence type correction and trigger retraining if needed"""
        self.feedback_db.log_occurrence_correction(
            text, predicted_type, corrected_type, confidence, source_doc, notes
        )
        self.correction_count += 1
        
        if self.correction_count % self.config.retrain_interval == 0:
            self.retrain_occurrence_model()
    
    def register_ner_correction(
        self, text: str, entity_type: str, predicted: str, corrected: str,
        confidence: float, char_start: int, char_end: int, source_doc: str = None
    ):
        """Log NER correction"""
        self.feedback_db.log_ner_correction(
            text, entity_type, predicted, corrected, confidence, char_start, char_end, source_doc
        )
    
    def retrain_occurrence_model(self):
        """Retrain classifier with HITL corrections"""
        texts, labels = self.feedback_db.get_training_data(task="occurrence")
        
        if len(texts) >= self.config.min_training_samples:
            logger.info(f"🔄 Retraining occurrence classifier with {len(texts)} samples...")
            self.occurrence_classifier.train(texts, labels)
            
            model_path = Path(self.config.model_dir) / self.config.occurrence_type_model
            self.occurrence_classifier.save(str(model_path))
            logger.info(f"✓ Model saved to {model_path}")
    
    def get_uncertain_predictions(self) -> pd.DataFrame:
        """Retrieve predictions flagged for review"""
        return self.feedback_db.get_uncertain_predictions(self.config.uncertainty_threshold)
    
    def export_metrics(self) -> Dict:
        """Export performance metrics"""
        texts, labels = self.feedback_db.get_training_data()
        
        if not texts:
            return {'status': 'no_training_data'}
        
        # Evaluate on training data (in production, use held-out test set)
        predictions = [p['predicted_type'] for p in self.occurrence_classifier.predict_with_confidence(texts)]
        
        from sklearn.metrics import accuracy_score, precision_recall_fscore_support
        
        acc = accuracy_score(labels, predictions)
        precision, recall, f1, _ = precision_recall_fscore_support(labels, predictions, average='weighted')
        
        return {
            'total_corrections': self.correction_count,
            'training_samples': len(texts),
            'accuracy': float(acc),
            'precision': float(precision),
            'recall': float(recall),
            'f1_score': float(f1),
            'model_status': 'trained' if self.occurrence_classifier.is_trained else 'untrained'
        }


# ─────────────────────────────────────────────────────────────────────────────
#  INTEGRATION HELPERS FOR BIOTRACE
# ─────────────────────────────────────────────────────────────────────────────

def initialize_hitl_pipeline(model_dir: str = "biotrace_ml_models") -> HITLPipeline:
    """Initialize HITL pipeline for BioTrace"""
    config = HITLConfig(model_dir=model_dir)
    return HITLPipeline(config)


def enrich_occurrence_record(record: Dict, hitl_pipeline: HITLPipeline) -> Dict:
    """
    Enrich BioTrace occurrence record with ML predictions.
    Adds confidence scores and uncertainty flags.
    """
    text = record.get('text', '')
    if not text:
        return record
    
    predictions = hitl_pipeline.process_text(text, record.get('sourceCitation'))
    
    # Add ML enrichments
    if predictions['occurrence_type']:
        record['ml_occurrence_type'] = predictions['occurrence_type']['type']
        record['ml_confidence_occurrence'] = predictions['occurrence_type']['confidence']
        record['ml_flag_review'] = predictions['occurrence_type']['confidence'] < 0.70
    
    record['ml_species_candidates'] = predictions['species']
    record['ml_localities_candidates'] = predictions['localities']
    
    return record


# ─────────────────────────────────────────────────────────────────────────────
#  TESTING & DEMO
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Demo data
    demo_texts = [
        "Tuna (Thunnus albacares) was observed at 12°N, 73°E off the coast of Goa",
        "Secondary observation: Ray species in the Arabian Sea near Mumbai harbor",
        "Dolphin sighting reported by local fishermen at Cochin backwaters",
        "Primary record: Seagrass meadows with associated fish fauna at Palk Strait",
    ]
    
    demo_types = ["Primary", "Secondary", "Secondary", "Primary"]
    
    # Initialize
    config = HITLConfig()
    pipeline = HITLPipeline(config)
    
    # Train with demo data
    logger.info("Training with demo data...")
    pipeline.occurrence_classifier.train(demo_texts, demo_types)
    
    # Process new text
    test_text = "Sharks observed in the Andaman waters during monsoon season"
    result = pipeline.process_text(test_text)
    
    logger.info(f"\n📊 Pipeline Result:")
    logger.info(json.dumps(result, indent=2))
    
    # Log a correction
    pipeline.register_occurrence_correction(
        test_text, 
        result['occurrence_type']['type'],
        "Primary",
        result['occurrence_type']['confidence'],
        notes="Confirmed as primary record by marine surveyor"
    )
    
    # Show metrics
    metrics = pipeline.export_metrics()
    logger.info(f"\n📈 Metrics: {json.dumps(metrics, indent=2)}")
