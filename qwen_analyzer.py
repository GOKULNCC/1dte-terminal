"""
Qwen 3.6-27B local analyzer — replaces ALL Claude API calls
Runs on your RTX 5090 via Ollama (OpenAI-compatible API)
Handles: sentiment, crash probability, pattern matching, predictions
"""
import json, requests, re
from datetime import datetime

from config import OLLAMA_URL, QWEN_MODEL as MODEL, db_connect
from event_window import should_block


def ask_qwen(prompt, think=True):
    """Call Qwen 3 via Ollama OpenAI-compatible API"""
    messages = [{"role": "user", "content": prompt}]
    # Qwen 3 thinks by default; use /no_think to disable
    if not think:
        messages[0]["content"] = "/no_think\n" + messages[0]["content"]
    
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": messages,
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 8000,
    })
    data = resp.json()
    msg = data["choices"][0]["message"]
    
    # Qwen 3 via Ollama returns thinking in 'reasoning' field, answer in 'content'
    thinking = msg.get("reasoning", "")
    answer = msg.get("content", "")
    
    # Fallback: check for inline <think> tags (older Ollama versions)
    if not thinking and "<think>" in answer and "</think>" in answer:
        thinking = answer.split("<think>")[1].split("</think>")[0].strip()
        answer = answer.split("</think>")[1].strip()
    
    return {"thinking": thinking, "answer": answer, "full": (thinking or "") + "\n" + answer}


def extract_json(text):
    """Extract JSON from model output"""
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    m = re.search(r'[\[{].*[}\]]', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except:
            pass
    return None


def score_headlines():
    """Score all unscored headlines with Qwen 3.6"""
    conn = db_connect(row_factory=True)
    
    # Get unscored headlines
    rows = conn.execute("""
        SELECT n.id, n.headline, n.source FROM news n
        LEFT JOIN scored_news s ON n.id = s.id
        WHERE s.id IS NULL
        ORDER BY n.fetched_at DESC LIMIT 20
    """).fetchall()
    
    if not rows:
        print("  No new headlines to score")
        return []
    
    # Batch score all headlines in one call (efficient)
    headlines_text = "\n".join([f"{i+1}. [{r['source']}] {r['headline']}" for i, r in enumerate(rows)])
    
    result = ask_qwen(f"""Score these market news headlines for a 1-DTE credit spread trader (sells 2-3% OTM puts on SPX/NDX).

HEADLINES:
{headlines_text}

For each headline, return a JSON array:
[{{"id": 1, "sentiment": -1.0 to 1.0, "category": "TARIFF|GEOPOLITICAL|FED_HAWK|OIL_SHOCK|EARNINGS|INFLATION|TECH|POLITICAL|MARKET", "crash_relevance": 0.0 to 1.0, "political": true/false, "author": "name or null", "note": "one-line trading insight"}}]

Important: sentiment -1 = very bearish, +1 = very bullish. crash_relevance = how likely this could cause SPX >-2% move.
Return ONLY the JSON array.""", think=True)
    
    scored = extract_json(result["answer"])
    if not scored or not isinstance(scored, list):
        print("  Failed to parse Qwen output")
        return []
    
    now = datetime.now().isoformat()
    saved = []
    for i, s in enumerate(scored):
        if i >= len(rows):
            break
        row = rows[i]
        conn.execute("INSERT OR REPLACE INTO scored_news VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (row["id"], row["headline"], row["source"],
                      s.get("sentiment", 0), s.get("category", "MARKET"),
                      s.get("crash_relevance", 0), 1 if s.get("political") else 0,
                      s.get("author"), s.get("note", ""), now))
        saved.append({**dict(row), **s})
    
    conn.commit()
    print(f"  Scored {len(saved)} headlines with Qwen 3.6")
    print(f"  Thinking depth: {len(result['thinking'])} chars")
    
    conn.close()
    return saved


def score_socials():
    """Score unscored political socials for market impact"""
    print("\n[Qwen] Scoring political socials...")
    conn = db_connect(row_factory=True)
    
    rows = conn.execute("SELECT * FROM political_socials WHERE qwen_sentiment = 'UNSCORED' ORDER BY created_at DESC LIMIT 15").fetchall()
    
    if not rows:
        print("  No new socials to score")
        return
        
    socials_text = "\n".join([f"{i+1}. [{r['author']}] {r['text']}" for i, r in enumerate(rows)])
    
    result = ask_qwen(f"""Score these political social media posts for their direct market impact on the S&P 500 or Nasdaq.
    
POSTS:
{socials_text}

For each post, return a JSON array exactly matching the input order:
[{{"id": 1, "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL"}}]

Rule: Only classify as BULLISH or BEARISH if there is a clear, direct market catalyst (e.g., tariffs, taxes, fed policy, war). Everything else is NEUTRAL.
Return ONLY the JSON array.""", think=True)

    scored = extract_json(result["answer"])
    if not scored or not isinstance(scored, list):
        print("  Failed to parse Qwen output for socials")
        return
        
    for i, s in enumerate(scored):
        if i >= len(rows): break
        sentiment = s.get("sentiment", "NEUTRAL")
        if sentiment not in ["BULLISH", "BEARISH"]:
            sentiment = "NEUTRAL"
            
        conn.execute("UPDATE political_socials SET qwen_sentiment = ? WHERE id = ?", (sentiment, rows[i]["id"]))
        
    conn.commit()
    print(f"  Scored {len(scored)} socials.")
    conn.close()

# --- distribution buckets (signed, cover the real line) -------------------
# Keys are stable IDs; order matters for display only.
BUCKETS = [
    "down_2plus", "down_1_5", "down_1", "down_0_5",
    "flat",
    "up_0_5", "up_1", "up_1_5", "up_2plus",
]
BUCKET_LABELS = {
    "down_2plus": "<= -2.0%",
    "down_1_5":   "-2.0% .. -1.5%",
    "down_1":     "-1.5% .. -1.0%",
    "down_0_5":   "-1.0% .. -0.5%",
    "flat":       "-0.5% .. +0.5%",
    "up_0_5":     "+0.5% .. +1.0%",
    "up_1":       "+1.0% .. +1.5%",
    "up_1_5":     "+1.5% .. +2.0%",
    "up_2plus":   ">= +2.0%",
}


def classify_bucket(pct: float) -> str:
    """Classify a realized close-to-close % move into one of BUCKETS."""
    if pct <= -2.0:    return "down_2plus"
    if pct <= -1.5:    return "down_1_5"
    if pct <= -1.0:    return "down_1"
    if pct <= -0.5:    return "down_0_5"
    if pct <   0.5:    return "flat"
    if pct <   1.0:    return "up_0_5"
    if pct <   1.5:    return "up_1"
    if pct <   2.0:    return "up_1_5"
    return "up_2plus"


def _ensure_predictions_schema(conn) -> None:
    """Add new columns idempotently so old DBs keep working."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    additions = [
        ("direction",          "TEXT"),
        ("expected_move_pct",  "REAL"),
        ("max_upside_pct",     "REAL"),
        ("max_downside_pct",   "REAL"),
        ("ndx_upside_pct",     "REAL"),
        ("ndx_downside_pct",   "REAL"),
        ("distribution_json",  "TEXT"),
        ("drivers_json",       "TEXT"),
        ("verdict",            "TEXT"),
        ("action",             "TEXT"),
        ("event_block_reason", "TEXT"),
    ]
    for col, ddl in additions:
        if col not in cols:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {ddl}")
    conn.commit()


def _normalize_distribution(raw: dict) -> dict:
    """Coerce missing keys to 0, clamp to [0,1], renormalize to sum=1."""
    cleaned = {}
    for k in BUCKETS:
        try:
            v = float(raw.get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        cleaned[k] = max(0.0, min(1.0, v))
    total = sum(cleaned.values())
    if total <= 0:
        # Degenerate output → fall back to ~base-rate-ish distribution centered on flat.
        cleaned = {k: 0.0 for k in BUCKETS}
        cleaned["flat"] = 1.0
        return cleaned
    return {k: v / total for k, v in cleaned.items()}


def predict_move():
    """Read news + scored political socials + indices → emit a signed move distribution."""
    conn = db_connect(row_factory=True)
    _ensure_predictions_schema(conn)

    news = conn.execute(
        "SELECT * FROM scored_news ORDER BY scored_at DESC LIMIT 20"
    ).fetchall()
    socials = conn.execute(
        "SELECT * FROM political_socials WHERE qwen_sentiment IN ('BULLISH','BEARISH') "
        "ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    indices = conn.execute("SELECT * FROM indices ORDER BY fetched_at DESC").fetchall()

    news_text = "\n".join([
        f"[{r['sentiment']:+.2f} {r['category']}] {r['headline']}"
        + (f" (POLITICAL: {r['author']})" if r['political'] else "")
        for r in news
    ]) or "No news yet"

    socials_text = "\n".join([
        f"[{r['qwen_sentiment']}] @{r['handle']} ({r['author']}): {r['text'][:240]}"
        for r in socials
    ]) or "No scored political posts"

    idx_text = "\n".join([
        f"{r['name']}: {r['price']} ({r['change_pct']:+.2f}%)" for r in indices
    ]) or "Not yet fetched"

    bucket_block = "\n".join([f'  "{k}": 0.0,   // {BUCKET_LABELS[k]}' for k in BUCKETS])

    result = ask_qwen(f"""You are a careful quantitative strategist. Predict the SPX close-to-close % move for the NEXT trading session as a FULL PROBABILITY DISTRIBUTION over signed buckets. Base your view on the news and political/social posts below — extract actual catalysts, do not invent.

HISTORICAL BASE RATES (close-to-close SPX):
- |move| <= 0.5% on ~55% of days  (this is the boring default)
- |move| 0.5%–1% on ~25%
- |move| 1%–2% on ~13%
- |move| > 2% on ~7% (split asymmetrically: down >2% happens ~3.4%, up >2% ~3.6%)
- Daily mean is ~+0.04%, slightly positive drift.

INPUTS — read every line and ATTRIBUTE each catalyst to a specific source:

CURRENT WORLD INDICES (overnight tape, leads US open):
{idx_text}

SCORED NEWS HEADLINES (Qwen sentiment, -1 bearish .. +1 bullish):
{news_text}

POLITICAL / TRUTH SOCIAL / X POSTS (already filtered for market relevance):
{socials_text}

Think deeply step by step:
1. List every CONCRETE catalyst from the inputs (skip vague chatter).
2. For political posts: distinguish "considering" / "studying" (low urgency) vs "effective immediately" / "signed today" (high urgency). A vague Truth Social rant is NOT a -2% catalyst.
3. Anchor on the base rates above. If there is no real catalyst, the distribution should look close to the base rates with mass concentrated on flat / +/-0.5%.
4. Only push mass into the tails (|move| > 1.5%) when you can name the specific catalyst that justifies it.
5. Decide direction from the NET balance of catalysts.

Return ONLY this JSON (probabilities must sum to ~1.0):
{{
  "direction": "UP" | "DOWN" | "FLAT",
  "expected_move_pct": -2.0 to +2.0,        // signed point estimate for SPX
  "max_upside_pct":   0.0 to 5.0,           // realistic ceiling for SPX move
  "max_downside_pct": -5.0 to 0.0,          // realistic floor for SPX move (signed)
  "ndx_upside_pct":   0.0 to 6.0,           // same for NDX
  "ndx_downside_pct": -6.0 to 0.0,
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "distribution": {{
{bucket_block}
  }},
  "primary_driver": "one sentence naming THE dominant catalyst",
  "top_drivers": ["specific catalyst 1", "specific catalyst 2", "specific catalyst 3"],
  "verdict": "2-3 sentences: directional bias, magnitude, and what would invalidate it"
}}""", think=True)

    parsed = extract_json(result["answer"])
    if not parsed:
        print("  Failed to parse Qwen output")
        conn.close()
        return None

    dist = _normalize_distribution(parsed.get("distribution", {}))
    # Back-compat: keep crash_prob populated as P(down_2plus) * 100 so legacy reports still work.
    crash_prob_pct = round(dist["down_2plus"] * 100.0, 2)

    # Event-window auto-SKIP: regardless of what the model says, refuse to trade
    # within +/- 30 min of any high-impact USD event (CPI/FOMC/NFP/etc.). IV crush
    # and headline whipsaw make the model's directional read unreliable here.
    blocked, blocking_events = should_block()
    if blocked:
        names = ", ".join(f"{e['event']} @ {e['time_et']} ET" for e in blocking_events[:3])
        block_reason = f"Within +/-30 min of: {names}"
        action = "SKIP"
        print(f"\n[event-window] AUTO-SKIP — {block_reason}")
    else:
        block_reason = None
        action = "TRADE" if parsed.get("confidence", "LOW") in ("HIGH", "MEDIUM") else "WIDEN"

    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO predictions ("
        "crash_prob, primary_driver, top_risks, confidence, model, created_at, "
        "direction, expected_move_pct, max_upside_pct, max_downside_pct, "
        "ndx_upside_pct, ndx_downside_pct, distribution_json, drivers_json, verdict, "
        "action, event_block_reason"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            crash_prob_pct,
            parsed.get("primary_driver", ""),
            json.dumps(parsed.get("top_drivers", [])),
            parsed.get("confidence", "LOW"),
            "Qwen3-30B", now,
            parsed.get("direction", "FLAT"),
            float(parsed.get("expected_move_pct", 0) or 0),
            float(parsed.get("max_upside_pct", 0) or 0),
            float(parsed.get("max_downside_pct", 0) or 0),
            float(parsed.get("ndx_upside_pct", 0) or 0),
            float(parsed.get("ndx_downside_pct", 0) or 0),
            json.dumps(dist),
            json.dumps(parsed.get("top_drivers", [])),
            parsed.get("verdict", ""),
            action,
            block_reason,
        ),
    )
    conn.commit()

    print(f"\n{'='*60}")
    print(f"ACTION:    {action}" + (f"  ({block_reason})" if block_reason else ""))
    print(f"DIRECTION: {parsed.get('direction', '?')}    "
          f"E[move]: {parsed.get('expected_move_pct', '?')}%    "
          f"confidence: {parsed.get('confidence', '?')}")
    print(f"SPX range: {parsed.get('max_downside_pct', '?')}% .. {parsed.get('max_upside_pct', '?')}%")
    print(f"NDX range: {parsed.get('ndx_downside_pct', '?')}% .. {parsed.get('ndx_upside_pct', '?')}%")
    print(f"DRIVER:    {parsed.get('primary_driver', '?')}")
    print(f"VERDICT:   {parsed.get('verdict', '?')}")
    print("Distribution:")
    for k in BUCKETS:
        bar = "#" * int(round(dist[k] * 40))
        print(f"  {BUCKET_LABELS[k]:>18}  {dist[k]*100:5.1f}%  {bar}")
    print(f"{'='*60}")
    print(f"Thinking depth: {len(result['thinking'])} chars\n")

    conn.close()
    return {**parsed, "distribution": dist}


# Back-compat alias so any external caller / scheduler still works.
def predict_crash():
    return predict_move()


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scoring headlines with Qwen 3.6-27B...")
    score_headlines()
    score_socials()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running crash prediction with deep thinking...")
    predict_crash()
