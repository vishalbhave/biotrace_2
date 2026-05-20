import sqlite3
import random

def get_cal_examples(db_path, correction_type, limit=5):
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cal_feedback'")
        if not cur.fetchone():
            return []
        cur.execute("SELECT original_text, bad_extraction, good_extraction FROM cal_feedback WHERE correction_type = ? ORDER BY created_at DESC LIMIT ?", (correction_type, limit * 2))
        rows = cur.fetchall()
        con.close()
        if not rows:
            return []
        results = [dict(row) for row in rows]
        random.shuffle(results)
        return results[:limit]
    except Exception as e:
        return []

def format_cal_prompt_section(correction_type, db_path='biotrace_occurrences.db'):
    examples = get_cal_examples(db_path, correction_type)
    if not examples:
        return ""
    prompt = "\n\n=== PAST EXTRACTION ERRORS TO AVOID ===\n"
    for i, ex in enumerate(examples):
        prompt += f"Example {i+1}:\nRaw Text: \"{ex['original_text']}\"\nBad: \"{ex['bad_extraction']}\"\nCorrect: \"{ex['good_extraction']}\"\n"
    prompt += "=======================================\n\n"
    return prompt
