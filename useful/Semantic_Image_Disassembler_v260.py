import gradio as gr
import base64
import os
import json
import urllib.request
import urllib.error
import time

# ---------------- CONFIGURATION ---------------- #
API_URL = "http://127.0.0.1:1234/v1" 
API_KEY = "lm-studio"

# ---------------- CSS ---------------- #
CSS = """
/* NUCLEAR OPTION: HIDE DEFAULT GRADIO LOADERS */
.clean-component .loading,
.clean-component .loading-label,
.clean-component .pending,
.clean-component .generating,
.clean-component .wrap .status,
.clean-component .status-tracker {
    display: none !important;
    opacity: 0 !important;
    visibility: hidden !important;
    transition: none !important;
    height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    content: "" !important;
}

/* MONITOR STYLING */
.monitor-box {
    display: block !important;
    background-color: #1e1e2e; 
    color: #cdd6f4; 
    padding: 15px 20px; 
    border-radius: 8px; 
    border: 1px solid #45475a; 
    font-family: 'Consolas', 'Monaco', monospace;
    margin-bottom: 10px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.3);
}
.monitor-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #45475a;
    padding-bottom: 10px;
    margin-bottom: 10px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
}
.monitor-title {
    color: #cd632a; /* MATCHING ORANGE ACCENT */
}
.status-indicator { font-weight: bold; font-size: 0.9em; }
.status-online { color: #a6e3a1; }
.status-offline { color: #f38ba8; }
.pipeline-mode { font-size: 1.1em; font-weight: bold; color: #89b4fa; margin-bottom: 5px; }
.pairing-info { 
    font-size: 0.9em; 
    color: #a6adc8; 
    display: flex; 
    justify-content: space-between; 
}
.progress-text { color: #a6e3a1; font-weight: bold; }

/* CUSTOM STEALTH PROGRESS BAR */
.monitor-progress-track {
    background-color: #313244;
    height: 8px; 
    border-radius: 4px;
    margin-top: 10px;
    overflow: hidden;
}
.monitor-progress-bar {
    background-color: #a6e3a1; 
    height: 100%;
    width: 0%;
    transition: width 0.3s ease;
}

/* LIVE FEED DASHBOARD (3-Column Grid) */
.live-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
    height: 300px;
    background-color: #181825;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid #313244;
}
.live-col {
    background-color: #11111b;
    border: 1px dashed #585b70;
    border-radius: 6px;
    padding: 10px;
    display: flex;
    flex-direction: column;
    overflow: hidden; 
    height: 100%; 
    min-height: 0;
}
.live-header {
    font-family: 'Consolas', monospace;
    font-weight: bold;
    color: #89b4fa;
    border-bottom: 1px solid #45475a;
    margin-bottom: 8px;
    padding-bottom: 4px;
    text-transform: uppercase;
    font-size: 0.85em;
    display: flex; 
    justify-content: space-between; 
    flex-shrink: 0;
}
.live-content {
    font-family: 'Consolas', monospace;
    font-size: 0.75em;
    color: #a6e3a1;
    overflow-y: auto;
    flex-grow: 1;
    min-height: 0;
    white-space: pre-wrap; 
    scrollbar-width: thin;
    scrollbar-color: #45475a #11111b;
    line-height: 1.4;
    padding-right: 5px;
}
.stat-tag {
    color: #cd632a; /* MATCHING ORANGE STATS */
    font-size: 0.9em;
}
"""

# ---------------- JSON SCHEMA ---------------- #
EYE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "visual_analysis_report",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "1_rendering_physics": {
                    "type": "object",
                    "properties": {
                        "medium_and_toolset": { "type": "string" },
                        "lighting_calculation": { "type": "string" },
                        "color_grading_palette": { "type": "string" },
                        "edge_and_line_quality": { "type": "string" },
                        "surface_artifacts": { "type": "string" }
                    },
                    "required": ["medium_and_toolset", "lighting_calculation", "color_grading_palette", "edge_and_line_quality", "surface_artifacts"],
                    "additionalProperties": False
                },
                "2_scene_structure": {
                    "type": "object",
                    "properties": {
                        "camera_angle": { "type": "string" },
                        "shot_type": { "type": "string" },
                        "compositional_balance": { "type": "string" },
                        "depth_and_focus": { "type": "string" }
                    },
                    "required": ["camera_angle", "shot_type", "compositional_balance", "depth_and_focus"],
                    "additionalProperties": False
                },
                "3_semantic_content": {
                    "type": "object",
                    "properties": {
                        "subject_identity": { "type": "string" },
                        "pose_and_action": { "type": "string" },
                        "expression_and_emotion": { "type": "string" },
                        "environment_setting": { "type": "string" },
                        "key_props": { "type": "string" }
                    },
                    "required": ["subject_identity", "pose_and_action", "expression_and_emotion", "environment_setting", "key_props"],
                    "additionalProperties": False
                },
                "4_artistic_genre_label": { "type": "string" }
            },
            "required": ["1_rendering_physics", "2_scene_structure", "3_semantic_content", "4_artistic_genre_label"],
            "additionalProperties": False
        }
    }
}

# ---------------- PROMPTS ---------------- #
PROMPT_STYLE_DISTILLER = """
**TASK:** You are a Style Distillation Engine.
**INPUT:** A full scene description.
**GOAL:** Extract strictly the **Visual DNA** (Style, Medium, Lighting, Palette).
**RULES:**
1. **STRIP** all Subjects, Actions, and Specific Objects (e.g., remove "man walking", "dog", "spaceship").
2. **KEEP** only the aesthetic adjectives and technical rendering terms.
3. **OUTPUT:** A concise, comma-separated block of style tags.
"""

PROMPT_DREAMER_TRANSFER = """
You are The Dreamer, an Advanced Visual Synthesis Engine.
**INPUTS:**
1. **VISUAL WIREFRAME:** (Subject/Action/Object).
2. **STYLE SHELL:** (Aesthetics/Physics).
**TASK:** Render the **WIREFRAME** using exclusively the physics of the **STYLE SHELL**.
**LOGIC:**
* **Subject Integrity:** Keep the Subject/Action/Position/Pose from the Wireframe.
* **Style Application:** Apply the lighting/texture of the Shell.
* **Conflict:** Style colors override Subject colors.
* **Positive Framing:** Describe what IS visible.
"""

PROMPT_DREAMER_STANDARD = """
**CONTEXT:** The input text is a "TL;DR" summary of a complex, high-fidelity image. It contains only the key aspects (the "Skeleton").
**YOUR GOAL:** Reverse-engineer the full-size, detailed description. Use input as a seed and reasoning to deduce and visualize full picture and only then describe it.
**OUTPUT:** A vivid, detailed description of the result scene. Write as if you are seeing this image in 4K UHD HDR.
"""

PROMPT_REFINER = """
You are The Refiner.
**TASK:** Synthesize inputs into a **Single, concise, high-density, Fluid Paragraph** of evocative visual prose.
**RULES:** Descriptive, Cinematic, Vivid. No labels.
**NO FLUFF:** Remove "you feel," "you see,"  metaphors, or emotional narration, or any metadata "— **End Scene** —". Describe ONLY what is visually present.
**CONSTRAINT:** Limit output to approx. 200 words (300 tokens).
"""

# ---------------- LOGIC ---------------- #

def get_connection_status():
    try:
        with urllib.request.urlopen(f"{API_URL}/models", timeout=0.5) as response:
            if response.getcode() == 200: return True
    except: pass
    return False

def count_file_items(file_list):
    if not file_list: return 0, 0, ""
    total_items = 0
    details = []
    for f in file_list:
        try:
            filename = os.path.basename(f.name)
            ext = os.path.splitext(filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']:
                total_items += 1
            elif ext == '.txt':
                with open(f.name, "r", encoding="utf-8") as txt:
                    lines = [l for l in txt if l.strip()]
                    count = len(lines)
                    total_items += count
                    details.append(f"{filename} ({count} lines)")
        except: pass
    detail_str = ", ".join(details) if details else f"{len(file_list)} image(s)"
    return len(file_list), total_items, detail_str

def update_ui_state(style_files, content_files, radio_mode, progress=0.0):
    # This function handles both the Monitor HTML update AND the locking of the Mode buttons
    
    # 1. Logic for disabling Mode buttons if Content exists
    has_content = (content_files is not None) and (len(content_files) > 0)
    mode_interactive = not has_content # Disable if content exists
    
    # 2. Monitor Logic
    is_online = get_connection_status()
    conn_html = '<span class="status-indicator status-online">[ONLINE]</span>' if is_online else '<span class="status-indicator status-offline">[OFFLINE]</span>'
    s_files, s_ops, s_det = count_file_items(style_files)
    c_files, c_ops, c_det = count_file_items(content_files)
    
    mode_text = "[WAITING] WAITING FOR INPUTS..."
    pair_text = ""
    
    if s_files > 0 and c_files == 0:
        if radio_mode == "Style DNA Only":
            mode_text = "[ANALYSIS] Extracting Style DNA"
        else:
            mode_text = "[ANALYSIS] Extracting Full Prompts"
        pair_text = f"Source: {s_det}. Total Operations: {s_ops}"
            
    elif s_files == 0 and c_files > 0:
        mode_text = "[CREATION] De-Summarization"
        pair_text = f"Remastering from: {c_det}. Total Operations: {c_ops}"
        
    elif s_files > 0 and c_files > 0:
        mode_text = "[CREATION] Semantic Style Transfer (Hybrid)"
        if c_ops == 1 and s_ops == 1:
            pair_text = "[SINGLE PAIR] 1 Content -> 1 Style"
        elif c_ops == 1 and s_ops > 1:
            pair_text = f"[STUDY MODE] 1 Content x {s_ops} Styles (Variations)"
        elif c_ops > 1 and s_ops == 1:
            pair_text = f"[BATCH MODE] {c_ops} Contents -> 1 Style (Unity)"
        elif c_ops == s_ops:
            pair_text = f"[ZIPPER MODE] {c_ops} pairs (1-to-1 sequential)"
        else:
            ops = max(c_ops, s_ops)
            pair_text = f"[LOOP MODE] Processing {ops} operations (cycling inputs)"

    # Generate Progress Bar HTML
    pct = int(progress * 100)
    prog_html = ""
    pct_text = ""
    
    if pct > 0:
        pct_text = f'<span class="progress-text">[PROGRESS: {pct}%]</span>'
        prog_html = f"""
        <div class="monitor-progress-track">
            <div class="monitor-progress-bar" style="width: {pct}%;"></div>
        </div>
        """

    monitor_html = f"""
    <div class="monitor-box">
        <div class="monitor-header">
            <span class="monitor-title">Semantic Image Disassembler</span>
            {conn_html}
        </div>
        <div class="pipeline-mode">{mode_text}</div>
        <div class="pairing-info">
            <span>{pair_text}</span>
            {pct_text}
        </div>
        {prog_html}
    </div>
    """
    
    return monitor_html, gr.update(interactive=mode_interactive)

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# --- BARE METAL API ENGINE ---
def send_api_request(messages, max_tokens=1000, temperature=0.7, response_format=None):
    url = f"{API_URL}/chat/completions"
    payload = {
        "model": "qwen3-vl-instruct",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False
    }
    if response_format: payload["response_format"] = response_format
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode('utf-8'))
        content = result['choices'][0]['message']['content']
        # Extract usage for TPS calculation
        usage = result.get('usage', {})
        total_tokens = usage.get('total_tokens', 0)
        return content, total_tokens

def analyze_with_schema(file_path):
    base64_img = encode_image(file_path)
    messages = [
        {"role": "system", "content": "You are The Architect."},
        {"role": "user", "content": [{"type": "text", "text": "Analyze."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}]}
    ]
    content, tokens = send_api_request(messages, max_tokens=2000, temperature=0.1, response_format=EYE_SCHEMA)
    return json.loads(content), tokens

def send_text_request(messages, max_tokens=1000):
    return send_api_request(messages, max_tokens=max_tokens, temperature=0.7)

def render_live_dashboard(eye_txt, dreamer_txt, refiner_txt, eye_stats="", dreamer_stats="", refiner_stats=""):
    def clean(text):
        if not text: return "..."
        return text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    return f"""
    <div class="live-grid">
        <div class="live-col">
            <div class="live-header">
                <span>I. THE EYE (Analysis)</span>
                <span class="stat-tag">{eye_stats}</span>
            </div>
            <div class="live-content">{clean(eye_txt)}</div>
        </div>
        <div class="live-col">
            <div class="live-header">
                <span>II. THE DREAMER (Synthesis)</span>
                <span class="stat-tag">{dreamer_stats}</span>
            </div>
            <div class="live-content">{clean(dreamer_txt)}</div>
        </div>
        <div class="live-col">
            <div class="live-header">
                <span>III. THE REFINER (Polish)</span>
                <span class="stat-tag">{refiner_stats}</span>
            </div>
            <div class="live-content">{clean(refiner_txt)}</div>
        </div>
    </div>
    """

def run_pipeline(input_files, style_ref_files, extract_mode, p_dreamer_trans, p_dreamer_std, p_refiner):
    # Dummy call to ensure mode button state is maintained properly in output
    monitor_init, mode_update = update_ui_state(style_ref_files, input_files, extract_mode)
    
    if not get_connection_status():
        yield "ERROR: Connection Lost.", None, None, monitor_init, "**OFFLINE**", mode_update
        return

    results_text = ""
    output_filename = "prompts_v26_0_baremetal.txt"
    with open(output_filename, "w", encoding="utf-8") as f: pass

    dash_state = {"eye": "Waiting...", "dreamer": "Waiting...", "refiner": "Waiting..."}
    stats_state = {"eye": "", "dreamer": "", "refiner": ""}
    
    current_prog_val = 0.0
    current_img_display = None

    def calc_stats(start_t, tokens):
        elapsed = time.time() - start_t
        if elapsed < 0.1: elapsed = 0.1
        tps = tokens / elapsed
        return f"Avg. TPS {tps:.1f} | {elapsed:.1f} s"

    def yield_update(log_append=None, prog_val=None):
        nonlocal results_text, current_prog_val
        if log_append: results_text += log_append
        if prog_val is not None: current_prog_val = prog_val
        
        mon_html, m_upd = update_ui_state(style_ref_files, input_files, extract_mode, progress=current_prog_val)
        
        return (
            results_text, 
            output_filename, 
            current_img_display, 
            mon_html, 
            render_live_dashboard(
                dash_state["eye"], dash_state["dreamer"], dash_state["refiner"],
                stats_state["eye"], stats_state["dreamer"], stats_state["refiner"]
            ),
            m_upd
        )

    # ---------------- MODE 1: ANALYSIS ONLY ----------------
    if style_ref_files and not input_files:
        s_files, total_ops, s_det = count_file_items(style_ref_files)
        completed_ops = 0
        
        for file_obj in style_ref_files:
            filename = os.path.basename(file_obj.name)
            ext = os.path.splitext(filename)[1].lower()
            
            try:
                # --- IMAGE HANDLING ---
                if ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']:
                    loop_prog = completed_ops / total_ops
                    dash_state["eye"] = f"Scanning {filename} physics..."
                    stats_state["eye"] = "" # Reset stats
                    current_img_display = file_obj.name
                    yield yield_update(prog_val=loop_prog)
                    
                    t_start = time.time()
                    data, tokens = analyze_with_schema(file_obj.name)
                    stats_state["eye"] = calc_stats(t_start, tokens)
                    
                    if extract_mode == "Style DNA Only":
                        physics = data.get("1_rendering_physics", {})
                        block = f"[STYLE DNA: {filename}]\nMEDIUM: {physics.get('medium_and_toolset')}\nLIGHTING: {physics.get('lighting_calculation')}\nCOLORS: {physics.get('color_grading_palette')}\n"
                        dash_state["eye"] = f"Extraction Complete:\n{block}"
                        yield yield_update(f"{block}\n\n", prog_val=loop_prog + (0.9/total_ops))
                    else:
                        json_name = f"{os.path.splitext(filename)[0]}_schema.json"
                        with open(json_name, "w", encoding="utf-8") as jf:
                            json.dump(data, jf, indent=4)
                            
                        manifest = (
                            f"GENRE: {data.get('4_artistic_genre_label')}\n"
                            f"PHYSICS: {data.get('1_rendering_physics')}\n"
                            f"STRUCTURE: {data.get('2_scene_structure')}\n"
                            f"CONTENT: {data.get('3_semantic_content')}"
                        )
                        dash_state["eye"] = f"Analysis Complete. JSON saved.\n{str(data.get('1_rendering_physics'))[:100]}..."
                        
                        dash_state["refiner"] = "Translating JSON to Prompt..."
                        stats_state["refiner"] = ""
                        yield yield_update(prog_val=loop_prog + (0.5/total_ops))
                        
                        t_start_ref = time.time()
                        refine_msg = [{"role": "system", "content": p_refiner}, {"role": "user", "content": f"TASK: Narratize this visual analysis data into a high-fidelity image prompt.\nDATA:\n{manifest}"}]
                        full_prompt, r_tokens = send_text_request(refine_msg)
                        full_prompt = full_prompt.replace("\n", " ").strip()
                        stats_state["refiner"] = calc_stats(t_start_ref, r_tokens)
                        
                        dash_state["refiner"] = f"Done.\n{full_prompt[:50]}..."
                        
                        with open(output_filename, "a", encoding="utf-8") as f: f.write(f"{full_prompt}\n")
                        yield yield_update(f"[FULL PROMPT: {filename}]\n{full_prompt}\n\n", prog_val=loop_prog + (0.99/total_ops))
                    
                    completed_ops += 1
                
                # --- TEXT HANDLING ---
                else:
                    dash_state["eye"] = f"Reading text file {filename}..."
                    current_img_display = None
                    yield yield_update(prog_val=completed_ops / total_ops)
                    
                    with open(file_obj.name, "r", encoding="utf-8") as f:
                        lines = [l.strip() for l in f if l.strip()]

                    for line_idx, line_content in enumerate(lines):
                        loop_prog = completed_ops / total_ops
                        safe_content = line_content[:4000].strip()
                        if not safe_content: continue
                        
                        if extract_mode == "Style DNA Only":
                            dash_state["eye"] = f"Distilling Style DNA from {filename} (Line {line_idx+1}/{len(lines)})..."
                            stats_state["eye"] = ""
                            yield yield_update(prog_val=loop_prog)
                            
                            t_start = time.time()
                            msg = [{"role": "system", "content": PROMPT_STYLE_DISTILLER}, {"role": "user", "content": safe_content}]
                            style_tags, tokens = send_text_request(msg)
                            stats_state["eye"] = calc_stats(t_start, tokens)
                            
                            yield yield_update(f"[STYLE DNA: {filename} L{line_idx+1}]\n{style_tags}\n\n", prog_val=loop_prog + (0.9/total_ops))
                            
                        else: 
                            dash_state["refiner"] = f"Refining {filename} (Line {line_idx+1}/{len(lines)})..."
                            stats_state["refiner"] = ""
                            yield yield_update(prog_val=loop_prog)
                            
                            t_start = time.time()
                            msg = [{"role": "system", "content": p_refiner}, {"role": "user", "content": f"TASK: Refine these notes into a high-fidelity image prompt.\nINPUT:\n{safe_content}"}]
                            full_prompt, tokens = send_text_request(msg)
                            full_prompt = full_prompt.replace("\n", " ").strip()
                            stats_state["refiner"] = calc_stats(t_start, tokens)
                            
                            with open(output_filename, "a", encoding="utf-8") as f: f.write(f"{full_prompt}\n")
                            yield yield_update(f"[FULL PROMPT: {filename} L{line_idx+1}]\n{full_prompt}\n\n", prog_val=loop_prog + (0.99/total_ops))
                        
                        completed_ops += 1

            except Exception as e:
                dash_state["eye"] = f"ERROR: {str(e)}"
                yield yield_update(f"[Error processing {filename}: {str(e)}]\n\n")
                if ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']: completed_ops += 1
        
        yield yield_update(prog_val=1.0)
        return

    # ---------------- MODE 2: CREATION (Hybrid) ----------------
    if not input_files and not style_ref_files:
         mon, m_upd = update_ui_state(None, None, extract_mode)
         yield "Waiting...", None, None, mon, render_live_dashboard("", "Waiting for inputs...", ""), m_upd
         return

    # 1. PREPARE STYLES
    all_styles = []
    if style_ref_files:
        total_style_files = len(style_ref_files)
        for idx, f in enumerate(style_ref_files):
            filename = os.path.basename(f.name)
            dash_state["eye"] = f"Extracting Style DNA from {filename}..."
            stats_state["eye"] = ""
            current_style_prog = (idx / total_style_files) * 0.1
            
            ext = os.path.splitext(filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']:
                current_img_display = f.name
            else:
                current_img_display = None
            
            yield yield_update(prog_val=current_style_prog)
            
            if ext == ".txt":
                try: 
                    with open(f.name, "r", encoding="utf-8") as txt: 
                        all_styles.extend([l.strip() for l in txt if l.strip()])
                except: pass
            elif ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp']:
                try:
                    t_start = time.time()
                    data, tokens = analyze_with_schema(f.name)
                    stats_state["eye"] = calc_stats(t_start, tokens)
                    
                    physics = data.get("1_rendering_physics", {})
                    dna_block = f"MEDIUM: {physics.get('medium_and_toolset')}, LIGHTING: {physics.get('lighting_calculation')}, PALETTE: {physics.get('color_grading_palette')}, TEXTURE: {physics.get('surface_artifacts')}"
                    all_styles.append(dna_block)
                except Exception as e:
                    yield yield_update(f"[Error extracting style from {filename}: {e}]\n")

    if not all_styles: all_styles = [None]

    # 2. PREPARE CONTENT
    all_content = []
    if input_files:
        for f in input_files:
             if f.name.endswith(".txt"):
                 try:
                     with open(f.name, "r") as txt: all_content.extend([(l.strip(), f.name) for l in txt if l.strip()])
                 except: pass
             else:
                 all_content.append((f.name, f.name))
    
    if not all_content: return

    # 3. PAIRING LOGIC
    total_ops = max(len(all_content), len(all_styles)) if len(all_content) == len(all_styles) else (len(all_styles) if len(all_content) == 1 else len(all_content))
    run_mode = "batch"
    if len(all_content) == 1 and len(all_styles) > 1: run_mode = "study" 
    elif len(all_content) > 1 and len(all_styles) == 1: run_mode = "batch" 
    elif len(all_content) == len(all_styles): run_mode = "zipper" 

    # 4. EXECUTION LOOP
    for i in range(total_ops):
        if run_mode == "study":
            cur_content = all_content[0]
            cur_style = all_styles[i]
        elif run_mode == "batch":
            cur_content = all_content[i]
            cur_style = all_styles[0]
        else:
            cur_content = all_content[i % len(all_content)]
            cur_style = all_styles[i % len(all_styles)]

        content_data, content_source = cur_content
        base_progress = 0.1 + ((i / total_ops) * 0.9)
        step_slice = (1 / total_ops) * 0.9 
        
        try:
            # PHASE 1: THE EYE
            manifest_text = ""
            ext = os.path.splitext(content_source)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp'] and content_data == content_source:
                 dash_state["eye"] = f"Analyzing geometry of {os.path.basename(content_source)}..."
                 stats_state["eye"] = ""
                 current_img_display = content_source
                 yield yield_update(prog_val=base_progress + (step_slice * 0.2))
                 
                 t_start = time.time()
                 data, tokens = analyze_with_schema(content_source)
                 stats_state["eye"] = calc_stats(t_start, tokens)
                 
                 struct = data.get("2_scene_structure", {})
                 content = data.get("3_semantic_content", {})
                 manifest_text = f"SCENE STRUCTURE:\n- Camera: {struct.get('camera_angle')}\n- Composition: {struct.get('compositional_balance')}\n\nSEMANTIC CONTENT:\n- Subject: {content.get('subject_identity')}\n- Action: {content.get('pose_and_action')}\n- Setting: {content.get('environment_setting')}"
                 dash_state["eye"] = manifest_text 
            else:
                 manifest_text = content_data
                 current_img_display = None
                 dash_state["eye"] = f"Text Input:\n{content_data[:100]}..."

            # PHASE 2: THE DREAMER
            dash_state["dreamer"] = "Synthesizing concept draft..."
            stats_state["dreamer"] = ""
            yield yield_update(prog_val=base_progress + (step_slice * 0.5))
            
            if cur_style:
                msgs = [{"role": "system", "content": PROMPT_DREAMER_TRANSFER}, {"role": "user", "content": f"VISUAL WIREFRAME:\n{manifest_text}\n\nSTYLE SHELL:\n{cur_style}"}]
            else:
                msgs = [{"role": "system", "content": PROMPT_DREAMER_STANDARD}, {"role": "user", "content": f"Here is the compressed manifest:\n{manifest_text}"}]
            
            t_start = time.time()
            dream_stream, tokens = send_text_request(msgs, max_tokens=1500)
            stats_state["dreamer"] = calc_stats(t_start, tokens)
            dash_state["dreamer"] = dream_stream 
            yield yield_update(prog_val=base_progress + (step_slice * 0.8))

            # PHASE 3: THE REFINER
            dash_state["refiner"] = "Polishing final prose..."
            stats_state["refiner"] = ""
            yield yield_update(prog_val=base_progress + (step_slice * 0.9))
            
            t_start = time.time()
            refine_msg = [{"role": "system", "content": PROMPT_REFINER}, {"role": "user", "content": f"DRAFT:\n{dream_stream}"}]
            final_prompt, tokens = send_text_request(refine_msg)
            final_prompt = final_prompt.replace("\n", " ").strip()
            stats_state["refiner"] = calc_stats(t_start, tokens)

            dash_state["refiner"] = final_prompt 
            with open(output_filename, "a", encoding="utf-8") as f: f.write(f"{final_prompt}\n")
            
            # End of item
            yield yield_update(f"{final_prompt}\n\n", prog_val=0.1 + ((i + 1) / total_ops) * 0.9)

        except Exception as e:
            dash_state["refiner"] = f"ERROR: {str(e)}"
            yield yield_update(f"Error: {str(e)}\n")
            
    # Final 100%
    yield yield_update(prog_val=1.0)

# ---------------- UI ---------------- #
with gr.Blocks(title="Semantic Image Disassembler (V26.0)") as demo:
    # Initialize UI Components
    monitor_html, _ = update_ui_state(None, None, "Style DNA Only")
    monitor = gr.HTML(value=monitor_html)
    live_status = gr.HTML(value=render_live_dashboard("", "Waiting for input...", ""), elem_classes="clean-component live-feed-box")
    
    with gr.Row():
        with gr.Column(scale=1):
            style_ref_files = gr.File(file_count="multiple", label="Styles")
            extract_mode = gr.Radio(["Style DNA Only", "Full Prompt Extraction"], label="Mode", value="Style DNA Only", elem_classes="clean-component")
            # CHANGED: Removed fixed height to support vertical aspect ratios
            active_preview = gr.Image(label="Active Monitor", interactive=False, elem_classes="clean-component")
            
        with gr.Column(scale=2):
            file_input = gr.File(file_count="multiple", label="Content")
            submit_btn = gr.Button("START PROCESSING", variant="primary", elem_classes="clean-component")
            output_log = gr.Textbox(label="Prompts", lines=10, autoscroll=True, elem_classes="clean-component")
            download_file = gr.File(label="Download", elem_classes="clean-component", height=50, interactive=False)
            with gr.Accordion("System Prompts", open=False):
                p_dreamer_trans = gr.Textbox(value=PROMPT_DREAMER_TRANSFER, label="Dreamer Transfer")
                p_dreamer_std = gr.Textbox(value=PROMPT_DREAMER_STANDARD, label="Dreamer Standard")
                p_refiner = gr.Textbox(value=PROMPT_REFINER, label="Refiner")

    # LISTENERS
    def wrap_update(s, c, m):
        return update_ui_state(s, c, m)

    for comp in [style_ref_files, file_input, extract_mode]:
        comp.change(fn=wrap_update, inputs=[style_ref_files, file_input, extract_mode], outputs=[monitor, extract_mode])
        
    # EVENT LISTENER
    submit_btn.click(
        fn=run_pipeline, 
        inputs=[file_input, style_ref_files, extract_mode, p_dreamer_trans, p_dreamer_std, p_refiner], 
        outputs=[output_log, download_file, active_preview, monitor, live_status, extract_mode],
        show_progress="hidden"
    )

if __name__ == "__main__":
    demo.launch(inbrowser=True, server_port=7861, css=CSS)