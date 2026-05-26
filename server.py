#!/usr/bin/env python3
"""
Screenplay Coverage Tool — Backend Server
Run with: python3 server.py
Then open: http://localhost:5000
"""

import os, re, json, time
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

try:
    import anthropic
except ImportError:
    print("[ERROR] Run: pip install anthropic flask flask-cors pdfplumber")
    exit(1)

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

app = Flask(__name__, static_folder=".")
CORS(app)

API_KEY = "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"
MODEL   = "claude-haiku-4-5-20251001"

SCENE_RE = re.compile(r"^\s*(INT\.|EXT\.|INT/EXT\.|EXT/INT\.)", re.IGNORECASE)

# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text(file_bytes, filename):
    suffix = filename.lower().split(".")[-1]
    if suffix == "pdf":
        if pdfplumber is None:
            raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")
        import io
        pages = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        return "\n".join(pages)
    elif suffix == "fdx":
        import xml.etree.ElementTree as ET
        root = ET.fromstring(file_bytes.decode("utf-8", errors="replace"))
        parts = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
        return "\n".join(parts)
    else:
        return file_bytes.decode("utf-8", errors="replace")

# ── Scene chunking ────────────────────────────────────────────────────────────

def chunk_screenplay(text):
    lines = text.splitlines()
    scene_starts = [i for i, ln in enumerate(lines) if SCENE_RE.match(ln)]

    if len(scene_starts) < 3:
        # fallback: character chunks
        size = 9000
        chunks = []
        for i in range(0, len(text), size):
            sl = text[i:i+size].strip()
            if len(sl) > 200:
                n = len(chunks)+1
                chunks.append({"text": sl[:13000], "scene_range": f"Block {n}",
                                "opening": f"Block {n}", "scene_count": 1})
        return chunks

    TARGET, MIN_SC = 9000, 3
    chunks, chunk_text, chunk_scenes, chunk_first = [], "", 0, 0

    for s_idx, line_idx in enumerate(scene_starts):
        end = scene_starts[s_idx+1] if s_idx+1 < len(scene_starts) else len(lines)
        chunk_text += ("\n" if chunk_text else "") + "\n".join(lines[line_idx:end])
        chunk_scenes += 1
        is_last   = (s_idx == len(scene_starts)-1)
        over_limit = (len(chunk_text) >= TARGET and chunk_scenes >= MIN_SC)
        if over_limit or is_last:
            chunks.append({
                "text":        chunk_text[:13000],
                "scene_range": f"Scenes {chunk_first+1}–{s_idx+1}",
                "opening":     lines[scene_starts[chunk_first]].strip()[:80],
                "scene_count": chunk_scenes,
            })
            chunk_text, chunk_scenes, chunk_first = "", 0, s_idx+1

    return chunks

# ── Claude helpers ────────────────────────────────────────────────────────────

def call_claude(client, system, user, max_tokens=1500):
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}]
            )
            return msg.content[0].text
        except Exception as e:
            if attempt == 2: raise
            time.sleep(3*(attempt+1))

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/coverage", methods=["POST"])
def generate_coverage():
    api_key = request.form.get("api_key", "").strip() or API_KEY
    if not api_key:
        return jsonify({"error": "No API key provided."}), 400

    script_type = request.form.get("script_type", "feature film")
    file        = request.files.get("screenplay")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400

    try:
        raw_bytes = file.read()
        text      = extract_text(raw_bytes, file.filename)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    if len(text) < 500:
        return jsonify({"error": "Could not extract enough text. Try a .txt or .fountain version."}), 400

    client = anthropic.Anthropic(api_key=api_key)
    chunks = chunk_screenplay(text)
    total_scenes = sum(c["scene_count"] for c in chunks)

    # Agent 2 — Summarise each chunk
    chunk_summaries = []
    for chunk in chunks:
        summary = call_claude(
            client,
            f"You are an expert script reader summarizing a section of a {script_type} screenplay. "
            "Each section starts and ends at a clean scene boundary. Be thorough but concise. "
            "Preserve: character names, key plot beats, tone, subtext, conflict, location changes, "
            "and notable craft choices. Do not editorialize.",
            f"Summarize this section ({chunk['scene_range']}, opening: {chunk['opening']}):\n\n{chunk['text']}",
            max_tokens=1200
        )
        chunk_summaries.append({"label": chunk["scene_range"], "opening": chunk["opening"], "summary": summary})

    # Agent 3 — Consolidate
    all_summaries = "\n\n---\n\n".join(
        f"BLOCK {i+1} ({c['label']}):\n{c['summary']}" for i, c in enumerate(chunk_summaries)
    )
    master = call_claude(
        client,
        f"You are an expert script analyst. Synthesize these section summaries of a {script_type} "
        "into one cohesive master summary. Cover: full story arc, main characters and arcs, themes, "
        "tone, key turning points, and resolution. Write in present tense, polished prose.",
        f"Synthesize:\n\n{all_summaries}",
        max_tokens=2000
    )

    # Agent 4 — Coverage
    comp_note = (
        "Comps must be TV shows/series unless there is a compelling justified reason to reference a film."
        if script_type == "TV pilot"
        else "Comps must be films unless there is a compelling justified reason to reference a TV show."
    )
    raw_cov = call_claude(
        client,
        "You are a professional script reader writing coverage. Respond ONLY with a valid JSON object. "
        "No markdown, no preamble.",
        f"""Write coverage for this {script_type} based on the master summary.

MASTER SUMMARY:
{master}

Return JSON with exactly these keys:
- "genre": genre(s) inferred from the summary
- "logline": compelling one or two sentence logline
- "email_response": professional paragraph(s) to client: why unique, what it did well, marketplace relevance, 1-2 comp titles with explanation. {comp_note}
- "summary": full master summary in clean polished prose, present tense

Respond ONLY with the JSON object.""",
        max_tokens=2500
    )

    try:
        coverage = json.loads(re.sub(r"```json|```", "", raw_cov).strip())
    except Exception:
        coverage = {"genre": "—", "logline": "—", "email_response": raw_cov[:1200], "summary": master}

    return jsonify({
        "coverage":        coverage,
        "chunk_summaries": chunk_summaries,
        "total_scenes":    total_scenes,
        "total_chunks":    len(chunks),
        "script_name":     file.filename,
        "script_type":     script_type,
    })

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  SCREENPLAY COVERAGE TOOL")
    print("  Open your browser to:  http://localhost:5000")
    print("="*55 + "\n")
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
