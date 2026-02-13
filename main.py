import os
import sys
import json
import sqlite3
import tarfile
import io
import requests
import subprocess
import threading
import datetime
import shutil
from PIL import Image
import customtkinter as ctk
from tkinter import filedialog, messagebox
import fitz  # PyMuPDF

# --- 全局配置 ---
# --- 全局配置 ---
ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

def get_resource_path(relative_path):
    """获取资源绝对路径，兼容开发环境和打包EXE环境"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

TECTONIC_PATH = get_resource_path("tectonic.exe")
CONFIG_FILE = "app_config.json"

# --- 1. 数据存储管理器 ---
class DataManager:
    def __init__(self):
        self.conn = None
        self.cursor = None
        self.config = self.load_config()
        self.init_db()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        # 默认配置，storage_path 默认为空，强制用户选择
        return {
            "storage_path": "", 
            "api_key": "", 
            "base_url": "https://api.openai.com/v1",
            "provider": "OpenAI",
            "model": "gpt-3.5-turbo",
            "clean_char": "-"
        }

    def save_config(self, new_config):
        self.config.update(new_config)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=4)
        self.init_db()

    def init_db(self):
        root = self.config["storage_path"]
        if not root: 
            return # 未设置路径时不初始化DB
            
        if not os.path.exists(root): os.makedirs(root)
        
        self.img_dir = os.path.join(root, "images")
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir)

        self.db_path = os.path.join(root, "library.db")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        
        # 建表：包含 packages 字段
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arxiv_id TEXT,
                latex_code TEXT,
                packages TEXT,
                note TEXT,
                image_filename TEXT,
                created_at TEXT
            )
        ''')
        # 自动迁移：防止旧数据库报错
        try:
            self.cursor.execute("SELECT packages FROM tables LIMIT 1")
        except sqlite3.OperationalError:
            self.cursor.execute("ALTER TABLE tables ADD COLUMN packages TEXT")
            self.conn.commit()
        self.conn.commit()

    def add_table(self, arxiv_id, latex_code, packages_list, image_src_path):
        if not self.cursor: return
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        img_filename = f"{arxiv_id}_{timestamp}.png"
        shutil.copy(image_src_path, os.path.join(self.img_dir, img_filename))
        
        packages_str = ",".join(packages_list)
        self.cursor.execute('''
            INSERT INTO tables (arxiv_id, latex_code, packages, note, image_filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (arxiv_id, latex_code, packages_str, "", img_filename, datetime.datetime.now().isoformat()))
        self.conn.commit()

    def get_all_tables(self):
        if not self.cursor: return []
        self.cursor.execute("SELECT * FROM tables ORDER BY created_at DESC")
        return self.cursor.fetchall()

    def update_note(self, table_id, new_note):
        if not self.cursor: return
        self.cursor.execute("UPDATE tables SET note = ? WHERE id = ?", (new_note, table_id))
        self.conn.commit()

    def delete_table(self, table_id):
        if not self.cursor: return
        self.cursor.execute("SELECT image_filename FROM tables WHERE id = ?", (table_id,))
        res = self.cursor.fetchone()
        if res:
            try: os.remove(os.path.join(self.img_dir, res[0]))
            except: pass
        self.cursor.execute("DELETE FROM tables WHERE id = ?", (table_id,))
        self.conn.commit()

# --- 2. 核心逻辑 ---
class CoreLogic:
    def fetch_arxiv_source(self, arxiv_id):
        url = f"https://arxiv.org/e-print/{arxiv_id}"
        response = requests.get(url)
        if response.status_code != 200: raise Exception("无法下载 arXiv 源码")
        
        source_code = ""
        try:
            with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith(".tex"):
                        f = tar.extractfile(member)
                        if f:
                            try: source_code += f"\n% --- {member.name} ---\n" + f.read().decode('utf-8', errors='ignore')
                            except: pass
        except: source_code = response.content.decode('utf-8', errors='ignore')
        return source_code

    def pre_scan_tables(self, source_code):
        """用正则预扫描源码，只查找原生 Table 环境（table, table*, sidewaystable, longtable）"""
        import re
        # 只匹配原生表格包裹环境，不匹配嵌套在 figure 等内部的独立 tabular
        env_pattern = re.compile(
            r'\\begin\{(table\*?|sidewaystable\*?|longtable\*?|supertabular\*?)\}'
        )
        
        lines = source_code.split('\n')
        results = []
        
        for line_no, line in enumerate(lines, 1):
            m = env_pattern.search(line)
            if m:
                env_name = m.group(1)
                # 向后搜索 caption 和 label
                caption = ""
                label = ""
                search_range = '\n'.join(lines[line_no-1:min(line_no+40, len(lines))])
                cap_m = re.search(r'\\caption\{([^}]*)\}', search_range)
                lab_m = re.search(r'\\label\{([^}]*)\}', search_range)
                if cap_m:
                    caption = cap_m.group(1)[:80]
                if lab_m:
                    label = lab_m.group(1)
                
                results.append({
                    'env': env_name,
                    'line': line_no,
                    'caption': caption,
                    'label': label,
                })
        
        return results

    def extract_and_analyze(self, api_key, base_url, source_code, provider="OpenAI", model="gpt-3.5-turbo", clean_mode=False, clean_char="-"):
        cleaning_instruction = ""
        if clean_mode:
            cleaning_instruction = f"Replace all specific numerical values in the table cells with '{clean_char}', but strictly preserve the headers, captions, and structural integrity."

        # === 正则预扫描 ===
        scan_results = self.pre_scan_tables(source_code)
        scan_count = len(scan_results)
        
        # 构建扫描报告
        scan_report = f"Pre-scan found {scan_count} table(s) in the source:\n"
        for i, r in enumerate(scan_results, 1):
            info = f"  #{i}: \\begin{{{r['env']}}} at line {r['line']}"
            if r['caption']:
                info += f"  caption=\"{r['caption']}\""
            if r['label']:
                info += f"  label={r['label']}"
            scan_report += info + "\n"
        
        print(f"\n[PRE-SCAN] {scan_report}")

        system_prompt = f"""You are a highly precise LaTeX Parsing Expert.
Your MISSION is to extract **EVERY single table** from the provided LaTeX source code. Do NOT skip any table.

### PRE-SCAN REFERENCE (auto-detected by regex):
{scan_report}
⚠️ You MUST extract AT LEAST {scan_count} table(s). If your output contains fewer tables than the pre-scan count, you are MISSING tables. Go back and find them.

### Scanning Rules:
1. Focus ONLY on native Table environments in the document:
   - `\\begin{{table}}`, `\\begin{{table*}}`
   - `\\begin{{sidewaystable}}`, `\\begin{{sidewaystable*}}`
   - `\\begin{{longtable}}`, `\\begin{{longtable*}}`
   - `\\begin{{supertabular}}`
2. Do NOT extract tabular data embedded inside `\\begin{{figure}}`, `\\begin{{minipage}}`, or other non-table environments.
3. Include tables in appendix and supplementary sections.
4. Extract ALL native Table environments without exception. Do not summarize, merge, or skip any.

### Completeness Verification:
Before finalizing your output, COUNT your extracted tables and compare with the pre-scan count ({scan_count}). 
- If you have FEWER tables than pre-scan, re-examine the source for missed tables.
- If tables are genuinely duplicated or empty, you may skip them, but note this in a brief comment.

### Data Cleaning:
{cleaning_instruction}

### CRITICAL Output Construction Rules:
For each table, generate a **valid, independently compilable** standalone LaTeX document.

**MANDATORY rules:**
1. Document Class: `\\documentclass[preview]{{standalone}}`
2. **NO floating environments**: Do NOT use `\\begin{{table}}`, `\\begin{{table*}}`, or `\\begin{{sidewaystable}}` in output. The `standalone` class does not support floats. Place `\\begin{{tabular}}` (or `longtable`/`tabularx`) directly inside `\\begin{{document}}`.
3. **NO captions or titles**: Remove ALL `\\caption{{...}}`, `\\label{{...}}`, `\\textbf{{Table N: ...}}` or any title text. Output ONLY the raw tabular body.
4. **Color Handling**:
   - Standard LaTeX colors (red, blue, green, yellow, cyan, magenta, black, white, gray, orange, purple, brown, darkgray, lightgray) should be kept AS IS.
   - Any CUSTOM-defined color must be REPLACED with the nearest standard LaTeX color.
   - Do NOT include any `\\definecolor` or `\\colorlet` commands in the output.
5. **Custom Commands**: If the source uses `\\newcommand` or `\\def` for symbols/macros used inside the table (e.g. `\\cmark`, `\\xmark`, `\\eg`), include those definitions in the preamble.
6. Packages: Include ALL necessary packages. Do NOT use the `transparent` package.
7. **NO transparency**: Remove ALL `\\transparent{{...}}` commands.
8. Remove `\\vspace`, `\\centering`, `\\label`, `\\caption` from the output.

### Output JSON Format:
Return a JSON object:
{{
    "tables_found": {scan_count},
    "tables_extracted": <actual number you extracted>,
    "tables": [
        {{
            "code": "\\\\documentclass[preview]{{standalone}}\\\\n\\\\usepackage{{booktabs}}\\\\n...\\\\begin{{document}}\\\\n\\\\begin{{tabular}}...\\\\end{{tabular}}\\\\n\\\\end{{document}}",
            "packages": ["booktabs", "xcolor"],
            "source_line": <approximate line number in original source>
        }}
    ]
}}
"""
        
        content_input = source_code[:100000]

        if provider == "Google":
            try:
                import google.generativeai as genai
                genai.configure(api_key=api_key)
                gemini_model = genai.GenerativeModel(model if model else "gemini-pro")
                response = gemini_model.generate_content(f"{system_prompt}\n\nUser Content:\n{content_input}")
                text_res = response.text
                if "```json" in text_res:
                    text_res = text_res.split("```json")[1].split("```")[0]
                elif "```" in text_res:
                    text_res = text_res.split("```")[1].split("```")[0]
                tables = json.loads(text_res).get('tables', [])
            except ImportError:
                 raise Exception("请安装 google-generativeai 库或使用 Compatible 模式")
            except Exception as e:
                raise Exception(f"Google API Error: {str(e)}")

        else:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)
            
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content_input}
                ],
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
                
            try:
                data = json.loads(content)
                tables = data.get('tables', [])
            except json.JSONDecodeError:
                print(f"JSON Parse Error. Raw Content:\n{content}")
                raise Exception("Model returned invalid JSON. Check console for details.")

        # === 提取后验证 ===
        extracted_count = len(tables)
        if extracted_count < scan_count:
            print(f"[WARN] ⚠️ LLM 提取了 {extracted_count} 个表格，但预扫描发现了 {scan_count} 个！可能有遗漏。")
        elif extracted_count > scan_count:
            print(f"[INFO] LLM 提取了 {extracted_count} 个表格（预扫描 {scan_count} 个），可能包含嵌套/拆分表格。")
        else:
            print(f"[INFO] ✅ LLM 提取数量 ({extracted_count}) 与预扫描 ({scan_count}) 一致。")
        
        return tables

    # Tectonic 不支持或 standalone 模式下不需要的宏包黑名单
    PACKAGE_BLACKLIST = {
        # Tectonic 兼容性问题
        'transparent', 'fontspec', 'unicode-math',
        # standalone 不需要的页面/文档级宏包
        'geometry', 'fancyhdr', 'titlesec', 'setspace', 'fullpage', 'a4wide',
        'parskip', 'tocbibind', 'tocloft', 'appendix', 'abstract', 'authblk',
        'footmisc', 'fancyvrb',
        # 参考文献 (standalone 无法处理)
        'natbib', 'biblatex', 'cite',
        # 浮动体和标题 (standalone 无浮动体)
        'caption', 'subcaption', 'float', 'placeins', 'wrapfig', 'subfig',
        # 算法/代码 (与表格无关)
        'algorithm', 'algorithmic', 'algpseudocode', 'algorithm2e',
        'listings', 'minted', 'verbatim',
        # 超链接 (standalone 不需要)
        'hyperref', 'cleveref', 'nameref',
        # 其他不相关
        'inputenc', 'fontenc', 'lmodern', 'times', 'palatino',
        'babel', 'polyglossia', 'csquotes',
        'enumitem', 'paralist',
        'lipsum', 'blindtext', 'comment',
        'etoolbox', 'ifthen', 'xifthen', 'ifpdf',
        'pdflscape', 'lscape', 'afterpage',
    }

    def extract_source_preamble(self, source_code):
        """从原始 LaTeX 源码中提取可复用的 preamble 元素"""
        import re
        packages = []   # (full_match, pkg_name, options)
        definitions = [] # 颜色定义、自定义命令等

        # 1. 提取 \usepackage（支持多包如 \usepackage{a,b,c}）
        for m in re.finditer(r'\\usepackage(\[[^\]]*\])?\{([^}]+)\}', source_code):
            options = m.group(1) or ""
            pkg_str = m.group(2)
            for pkg in pkg_str.split(','):
                pkg = pkg.strip()
                if pkg and pkg not in self.PACKAGE_BLACKLIST:
                    packages.append((pkg, options))

        # 2. 提取 \definecolor
        for m in re.finditer(r'\\definecolor\{[^}]+\}\{[^}]+\}\{[^}]+\}', source_code):
            definitions.append(m.group(0))

        # 3. 提取 \colorlet
        for m in re.finditer(r'\\colorlet\{[^}]+\}\{[^}]+\}', source_code):
            definitions.append(m.group(0))

        # 4. 提取简单的 \newcommand / \renewcommand / \providecommand（单行）
        for m in re.finditer(
            r'\\(?:newcommand|renewcommand|providecommand)\*?\{\\[a-zA-Z]+\}'
            r'(?:\[\d+\](?:\[[^\]]*\])?)?'
            r'\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}',
            source_code
        ):
            definitions.append(m.group(0))

        # 5. 提取 \DeclareMathOperator
        for m in re.finditer(r'\\DeclareMathOperator\*?\{\\[a-zA-Z]+\}\{[^}]+\}', source_code):
            definitions.append(m.group(0))

        # 6. 提取简单的 \def\cmd{...}
        for m in re.finditer(r'\\def\\[a-zA-Z]+\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', source_code):
            definitions.append(m.group(0))

        return packages, definitions

    def render_latex(self, latex_code, source_packages=None, source_definitions=None, api_config=None, original_source=None, status_cb=None):
        import re
        
        # === Step 1: 彻底清理模型输出，只保留 document body ===
        # 提取 \begin{document}...\end{document} 之间的内容
        body_match = re.search(r'\\begin\{document\}(.*?)\\end\{document\}', latex_code, re.DOTALL)
        if body_match:
            doc_body = body_match.group(1)
        else:
            # 没有 document 环境，整段就是 body
            doc_body = re.sub(r'\\documentclass(\[.*?\])?\{.*?\}\s*', '', latex_code)
        
        # === Step 2: 构建宏包列表（必备包 + 源码包，去重）===
        essential = [
            ('[table]', 'xcolor'),
            ('', 'booktabs'),
            ('', 'multirow'),
            ('', 'multicol'),
            ('', 'graphicx'),
            ('', 'array'),
            ('', 'makecell'),
            ('', 'amsmath'),
            ('', 'amssymb'),
            ('', 'textcomp'),
            ('', 'pifont'),
            ('', 'adjustbox'),
            ('', 'threeparttable'),
            ('', 'tabularx'),
            ('', 'longtable'),
            ('', 'hhline'),
            ('', 'colortbl'),
            ('', 'soul'),
            ('', 'ulem'),
            ('', 'bm'),
            ('', 'siunitx'),
        ]
        
        seen_pkgs = set()
        pkg_entries = []  # [(options, pkg_name), ...]
        
        for opts, pkg_name in essential:
            seen_pkgs.add(pkg_name)
            pkg_entries.append((opts, pkg_name))
        
        # 加入源码的额外包（已过滤黑名单）
        if source_packages:
            for pkg_name, opts in source_packages:
                if pkg_name not in seen_pkgs and pkg_name not in self.PACKAGE_BLACKLIST:
                    if pkg_name == 'xcolor':
                        continue
                    seen_pkgs.add(pkg_name)
                    pkg_entries.append((opts, pkg_name))
        
        # === Step 3: 源码中的颜色定义和自定义命令 ===
        def_lines = []
        if source_definitions:
            def_lines = list(dict.fromkeys(source_definitions))
        
        # === Step 4: 扫描 body 中的未知颜色，生成兜底定义 ===
        standard_colors = {
            'red', 'green', 'blue', 'cyan', 'magenta', 'yellow', 
            'black', 'white', 'darkgray', 'gray', 'lightgray',
            'brown', 'lime', 'olive', 'orange', 'pink', 'purple', 
            'teal', 'violet',
        }
        already_defined = set()
        for d in def_lines:
            m = re.search(r'\\(?:definecolor|colorlet)\{([^}]+)\}', d)
            if m:
                already_defined.add(m.group(1))
        
        for m in re.finditer(r'\\(?:rowcolor|cellcolor|textcolor|color)\{([^}]+)\}', doc_body):
            cname = m.group(1).split('!')[0].strip()
            if cname and cname not in standard_colors and cname not in already_defined:
                def_lines.append(f"\\definecolor{{{cname}}}{{HTML}}{{CCCCCC}}")
                already_defined.add(cname)
        
        # === Step 5: 命令 fallback ===
        fallback_cmds = [
            "\\providecommand{\\transparent}[1]{}",
            "\\providecommand{\\cite}[1]{[#1]}",
            "\\providecommand{\\cref}[1]{Ref.}",
            "\\providecommand{\\Cref}[1]{Ref.}",
            "\\providecommand{\\ref}[1]{??}",
            "\\providecommand{\\eqref}[1]{(??)}",
            "\\providecommand{\\url}[1]{#1}",
            "\\providecommand{\\href}[2]{#2}",
            "\\providecommand{\\cmark}{\\ding{51}}",
            "\\providecommand{\\xmark}{\\ding{55}}",
        ]
        
        # === Step 6: 自动重试编译（遇到 File not found 自动剥离该包）===
        max_retries = 10
        local_blacklist = set()
        last_full_tex = ""
        last_error_msg = ""
        _sc = status_cb or (lambda msg: None)  # status callback shorthand
        
        for attempt in range(max_retries + 1):
            # 过滤掉本轮被 ban 的包
            pkg_lines = []
            for opts, pkg_name in pkg_entries:
                if pkg_name not in local_blacklist:
                    pkg_lines.append(f"\\usepackage{opts}{{{pkg_name}}}")
            
            # 组装完整 .tex 文件
            full_tex = (
                "\\documentclass[preview]{standalone}\n"
                + "\n".join(pkg_lines) + "\n"
                + "\n".join(def_lines) + "\n"
                + "\n".join(fallback_cmds) + "\n"
                + "\\begin{document}\n"
                + doc_body + "\n"
                + "\\end{document}\n"
            )
            
            _sc("⚙️ Compiling...")
            success, img_path, error_msg = self._compile_tex(full_tex)
            if success:
                method = "AUTO" if local_blacklist else "DIRECT"
                if local_blacklist:
                    print(f"[AUTO-FIX] 自动移除了不可用的包: {local_blacklist}")
                return img_path, method
            
            last_full_tex = full_tex
            last_error_msg = error_msg
            
            # 匹配 "File `xxx.sty' not found" 或 "File `xxx.cls' not found"
            not_found = re.search(r"File `([^']+)\.(sty|cls)' not found", error_msg)
            if not_found and attempt < max_retries:
                missing = not_found.group(1)
                local_blacklist.add(missing)
                _sc(f"🔧 Auto-fix: removing '{missing}'")
                print(f"[AUTO-FIX] 包 '{missing}' 不可用，自动移除并重试 (attempt {attempt+1}/{max_retries})")
                import time; time.sleep(0.01)
                continue
            
            break  # 非 File-not-found 错误 → 跳出进入 LLM 修复阶段
        
        # === Step 7: LLM 辅助修复（最多 3 次）===
        if api_config and original_source:
            print(f"[LLM-FIX] 自动重试无法修复，启动 LLM 辅助修复...")
            current_tex = last_full_tex
            current_error = last_error_msg
            
            for llm_attempt in range(1, 4):  # 最多 3 次
                _sc(f"🤖 LLM Fix ({llm_attempt}/3)...")
                print(f"[LLM-FIX] 第 {llm_attempt}/3 次 LLM 修复尝试...")
                try:
                    fixed_tex = self.llm_fix_latex(
                        api_config, original_source, current_tex, current_error
                    )
                    if not fixed_tex:
                        print(f"[LLM-FIX] LLM 返回空内容，跳过")
                        break
                    
                    _sc(f"⚙️ Recompiling (LLM fix {llm_attempt})...")
                    success, img_path, error_msg = self._compile_tex(fixed_tex)
                    if success:
                        print(f"[LLM-FIX] ✅ 第 {llm_attempt} 次 LLM 修复成功！")
                        return img_path, f"LLM-{llm_attempt}"
                    
                    print(f"[LLM-FIX] 第 {llm_attempt} 次修复后仍编译失败")
                    current_tex = fixed_tex
                    current_error = error_msg
                    
                except Exception as llm_err:
                    print(f"[LLM-FIX] LLM 调用出错: {str(llm_err)[:200]}")
                    break
            
            print(f"[LLM-FIX] ❌ 3 次 LLM 修复均失败，放弃此表格")
        
        # 最终失败 → 打印调试信息并抛出异常
        lines_list = last_full_tex.split('\n')
        print(f"\n{'='*60}")
        print(f"[DEBUG] 最终编译失败的 LaTeX 源码:")
        print(f"{'='*60}")
        for i, line in enumerate(lines_list, 1):
            print(f"  {i:3d}: {line}")
        print(f"{'='*60}")
        print(f"[DEBUG] 最终 Tectonic Error: {last_error_msg[:500]}")
        print(f"{'='*60}\n")
        raise Exception(f"编译失败: {last_error_msg[:500]}...")

    def _compile_tex(self, full_tex):
        """编译 LaTeX 代码，返回 (success, img_path_or_None, error_msg)"""
        temp_id = datetime.datetime.now().strftime("%f")
        tex_file = f"temp_{temp_id}.tex"
        pdf_file = f"temp_{temp_id}.pdf"
        
        with open(tex_file, "w", encoding="utf-8") as f:
            f.write(full_tex)
        
        result = subprocess.run(
            [TECTONIC_PATH, tex_file],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        if os.path.exists(pdf_file):
            doc = fitz.open(pdf_file)
            pix = doc[0].get_pixmap(dpi=300)
            img_path = f"temp_{temp_id}.png"
            pix.save(img_path)
            try:
                os.remove(tex_file)
                os.remove(pdf_file)
            except: pass
            return True, img_path, ""
        
        error_msg = result.stderr.decode('utf-8', errors='ignore') + "\n" + result.stdout.decode('utf-8', errors='ignore')
        try: os.remove(tex_file)
        except: pass
        return False, None, error_msg

    def llm_fix_latex(self, api_config, original_source, failed_tex, error_msg):
        """调用 LLM 修复编译失败的 LaTeX 代码"""
        fix_prompt = """You are a LaTeX compilation error fixer.

Given:
1. The ORIGINAL LaTeX source document (for context/reference)
2. A standalone LaTeX file that FAILED to compile
3. The compilation error messages

Your task: Produce a CORRECTED, complete standalone LaTeX file that will compile successfully with the Tectonic engine.

STRICT Rules:
- Use `\\documentclass[preview]{standalone}`
- Only include packages available in standard CTAN/TeX distributions
- Do NOT use conference/journal style files (icml2025, neurips, nips, aaai, acl, tech2025, etc.)
- Do NOT use `transparent`, `fontspec`, `xeCJK`, `hyperref` packages
- Keep the table content and structure EXACTLY intact — do not change any data
- Fix undefined control sequences by providing \\providecommand fallbacks or removing them if decorative
- Fix any package conflicts or missing dependencies
- If a custom command uses unavailable packages, simplify it (e.g., \\scalebox → remove, \\rotatebox → remove, keep text content)
- Include the FULL corrected .tex file from \\documentclass to \\end{document}
- Return ONLY the corrected LaTeX code. No explanations, no markdown code fences, no comments outside the code."""

        user_content = f"""=== ORIGINAL SOURCE (excerpt, first 5000 chars) ===
{original_source[:5000]}

=== FAILED STANDALONE TEX FILE ===
{failed_tex}

=== COMPILATION ERRORS ===
{error_msg[:2000]}

Please produce the corrected standalone .tex file:"""

        provider = api_config.get('provider', 'OpenAI')
        api_key = api_config['api_key']
        base_url = api_config.get('base_url', '')
        model = api_config.get('model', 'gpt-3.5-turbo')

        if provider == "Google":
            try:
                import google.generativeai as genai
                genai.configure(api_key=api_key)
                gemini_model = genai.GenerativeModel(model if model else "gemini-pro")
                response = gemini_model.generate_content(f"{fix_prompt}\n\n{user_content}")
                result_text = response.text
            except Exception as e:
                raise Exception(f"Google API Error: {str(e)}")
        else:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": fix_prompt},
                    {"role": "user", "content": user_content}
                ],
            )
            result_text = response.choices[0].message.content

        # 清理可能的 markdown 代码块包装
        if "```latex" in result_text:
            result_text = result_text.split("```latex")[1].split("```")[0]
        elif "```tex" in result_text:
            result_text = result_text.split("```tex")[1].split("```")[0]
        elif "```" in result_text:
            parts = result_text.split("```")
            if len(parts) >= 3:
                result_text = parts[1]
        
        result_text = result_text.strip()
        
        # 验证返回内容包含基本 LaTeX 结构
        if "\\begin{document}" not in result_text or "\\end{document}" not in result_text:
            print(f"[LLM-FIX] LLM 返回内容不完整，缺少 document 环境")
            return None
        
        print(f"[LLM-FIX] LLM 返回了 {len(result_text)} 字符的修复代码")
        return result_text

# --- 3. UI 界面 ---
import webbrowser
TRANSLATIONS = {
    "CN": {
        "title": "Latex 表格提取器",
        "logo_text": "LT Miner",
        "api_group": "API 设置",
        "api_key_ph": "请输入 API Key",
        "base_url_ph": "Base URL (默认自动)",
        "model_ph": "模型名称 (e.g. gpt-4)",
        "path_btn": "更改存储路径",
        "import_btn": "导入本地 .tex 文件",
        "task_group": "新任务",
        "arxiv_ph": "ArXiv ID (e.g. 2301.xxxx)",
        "clean_mode": "数据脱敏模式",
        "clean_hint": "说明：开启后将数值替换为选定字符，用于清洗敏感数据。",
        "run_btn": "开始提取",
        "run_btn_loading": "处理中...",
        "tab_lib": "资料库",
        "tab_insp": "检查器",
        "pkg_label": "依赖包 (Packages)",
        "copy_btn": "复制引用代码",
        "src_label": "LaTeX 源码",
        "note_label": "备注",
        "save_note_btn": "保存备注",
        "preview_lost": "预览丢失",
        "preview_none": "无预览",
        "lib_view": "查看",
        "lib_del": "删除",
        "success_title": "成功",
        "success_msg": "提取 {} 个表格",
        "copy_success_title": "复制成功",
        "confirm_del": "确认删除此条目？",
        "warn_no_api": "请输入 API Key",
        "warn_no_url": "请输入 Base URL",
        "warn_no_path": "请先选择存储路径！",
        "copyright": "Copyright © 2ManyStars",
        "arrow_hint": "提示：在检查器中按 ↑↓ 方向键可快捷切换表格"
    },
    "EN": {
        "title": "Latex Table Miner",
        "logo_text": "LT Miner",
        "api_group": "API Settings",
        "api_key_ph": "Enter API Key",
        "base_url_ph": "Base URL (Auto default)",
        "model_ph": "Model Name (e.g. gpt-4)",
        "path_btn": "Change Storage Path",
        "import_btn": "Import Local .tex",
        "task_group": "New Task",
        "arxiv_ph": "ArXiv ID (e.g. 2301.xxxx)",
        "clean_mode": "Data Desensitization",
        "clean_hint": "Note: Replaces numerical data with selected char for privacy.",
        "run_btn": "Start Extraction",
        "run_btn_loading": "Processing...",
        "tab_lib": "Library",
        "tab_insp": "Inspector",
        "pkg_label": "Packages",
        "copy_btn": "Copy Command",
        "src_label": "LaTeX Source",
        "note_label": "Notes",
        "save_note_btn": "Save Note",
        "preview_lost": "Preview Lost",
        "preview_none": "No Preview",
        "lib_view": "View",
        "lib_del": "Delete",
        "success_title": "Success",
        "success_msg": "Extracted {} tables",
        "copy_success_title": "Copied",
        "confirm_del": "Delete this item?",
        "warn_no_api": "Please enter API Key",
        "warn_no_url": "Please enter Base URL",
        "warn_no_path": "Please select storage path first!",
        "copyright": "Copyright © 2ManyStars",
        "arrow_hint": "Tip: Press ↑↓ arrow keys in Inspector to switch tables"
    }
}

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.lang = "CN"
        self.t = TRANSLATIONS[self.lang]
        
        self.title(self.t["title"])
        self.geometry("1100x800")
        self.data_manager = DataManager()
        self.logic = CoreLogic()
        self.current_table_id = None
        self.library_data = []  # 存储当前 library 数据用于翻页
        self.current_index = -1  # 当前在 library_data 中的索引
        self.setup_ui()
        self.refresh_library()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # 字体颜色配置 (增强对比度)
        self.text_color_primary = "#1A1A1A"  # 深黑
        self.text_color_secondary = "#555555" # 深灰

        # 左侧 Sidebar
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        # 语言切换
        self.lang_switch = ctk.CTkSegmentedButton(self.sidebar, values=["CN", "EN"], command=self.change_language)
        self.lang_switch.set("CN")
        self.lang_switch.pack(pady=(20, 10), padx=15, fill="x")

        self.logo_label = ctk.CTkLabel(self.sidebar, text="LT Miner", font=("Roboto Medium", 22), text_color=self.text_color_primary)
        self.logo_label.pack(pady=(10, 20))
        
        # --- API 设置 ---
        self.api_group_label = ctk.CTkLabel(self.sidebar, text="API Settings", font=("Arial", 14, "bold"), anchor="w", text_color=self.text_color_primary)
        self.api_group_label.pack(padx=15, fill="x")
        
        self.provider_var = ctk.StringVar(value=self.data_manager.config.get("provider", "OpenAI"))
        self.provider_menu = ctk.CTkOptionMenu(self.sidebar, values=["OpenAI", "Google", "DeepSeek", "Qwen"], 
                                               variable=self.provider_var, command=self.update_provider_settings)
        self.provider_menu.pack(pady=5, padx=15, fill="x")

        self.api_input = ctk.CTkEntry(self.sidebar)
        self.api_input.insert(0, self.data_manager.config.get("api_key", ""))
        self.api_input.pack(pady=5, padx=15, fill="x")

        self.base_url_input = ctk.CTkEntry(self.sidebar)
        self.base_url_input.insert(0, self.data_manager.config.get("base_url", "https://api.openai.com/v1"))
        self.base_url_input.pack(pady=5, padx=15, fill="x")
        
        self.model_input = ctk.CTkEntry(self.sidebar)
        self.model_input.insert(0, self.data_manager.config.get("model", "gpt-3.5-turbo"))
        self.model_input.pack(pady=5, padx=15, fill="x")

        self.path_btn = ctk.CTkButton(self.sidebar, text="Path", command=self.change_path, fg_color="transparent", border_width=1, text_color=self.text_color_primary)
        self.path_btn.pack(pady=10, padx=15, fill="x")

        # --- 任务设置 ---
        self.task_group_label = ctk.CTkLabel(self.sidebar, text="New Task", font=("Arial", 14, "bold"), text_color=self.text_color_primary)
        self.task_group_label.pack(pady=(20, 5), anchor="w", padx=15)
        
        self.arxiv_input = ctk.CTkEntry(self.sidebar)
        self.arxiv_input.pack(pady=5, padx=15, fill="x")

        self.import_local_btn = ctk.CTkButton(self.sidebar, text="Import Local", command=self.import_local, fg_color="#5F6F81")
        self.import_local_btn.pack(pady=(0, 5), padx=15, fill="x")
        
        # 数据脱敏模块
        self.clean_mode_var = ctk.BooleanVar(value=False)
        self.clean_mode_checkbox = ctk.CTkCheckBox(self.sidebar, text="Clean Mode", variable=self.clean_mode_var, text_color=self.text_color_primary)
        self.clean_mode_checkbox.pack(pady=(10, 2), padx=15, anchor="w")
        
        self.clean_hint_label = ctk.CTkLabel(self.sidebar, text="Hint...", text_color=self.text_color_secondary, font=("Arial", 11), wraplength=200, justify="left")
        self.clean_hint_label.pack(padx=15, anchor="w")

        self.clean_char_var = ctk.StringVar(value=self.data_manager.config.get("clean_char", "-"))
        self.clean_char_seg = ctk.CTkSegmentedButton(self.sidebar, values=["-", "SPACE"], variable=self.clean_char_var)
        self.clean_char_seg.pack(pady=5, padx=15, fill="x")

        self.run_btn = ctk.CTkButton(self.sidebar, text="Run", command=self.start_extract_thread)
        self.run_btn.pack(pady=20, padx=15, fill="x")

        # Copyright Link
        self.copyright_label = ctk.CTkLabel(self.sidebar, text="Copyright © 2ManyStars", text_color="gray", cursor="hand2")
        self.copyright_label.pack(side="bottom", pady=10)
        self.copyright_label.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/DianxingShi"))

        # 右侧 Tabview
        self.tabview = ctk.CTkTabview(self, text_color=self.text_color_primary)
        self.tabview.grid(row=0, column=1, sticky="nsew", padx=15, pady=10)
        self.tabview.add("Library")
        self.tabview.add("Inspector")
        
        self.library_frame = ctk.CTkScrollableFrame(self.tabview.tab("Library"))
        self.library_frame.pack(fill="both", expand=True)
        
        # Inspector 界面
        self.inspector = ctk.CTkFrame(self.tabview.tab("Inspector"), fg_color="transparent")
        self.inspector.pack(fill="both", expand=True)
        
        self.insp_left = ctk.CTkFrame(self.inspector)
        self.insp_left.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        self.img_preview = ctk.CTkLabel(self.insp_left, text="No Preview", text_color="gray")
        self.img_preview.pack(expand=True)
        
        self.insp_right = ctk.CTkScrollableFrame(self.inspector, width=420)
        self.insp_right.pack(side="right", fill="y", padx=5, pady=5)
        
        # 依赖包区域
        self.pkg_label = ctk.CTkLabel(self.insp_right, text="Packages", font=("Arial", 14, "bold"), text_color=self.text_color_primary)
        self.pkg_label.pack(anchor="w", pady=(10,5))
        
        self.packages_frame = ctk.CTkFrame(self.insp_right, fg_color="transparent")
        self.packages_frame.pack(fill="x", pady=5)
        
        self.copy_pkg_btn = ctk.CTkButton(self.insp_right, text="Copy", height=24, command=self.copy_packages)
        self.copy_pkg_btn.pack(fill="x", pady=(0, 15))
        
        self.src_label = ctk.CTkLabel(self.insp_right, text="Source", font=("Arial", 14, "bold"), text_color=self.text_color_primary)
        self.src_label.pack(anchor="w")
        self.code_editor = ctk.CTkTextbox(self.insp_right, height=200, font=("Consolas", 12))
        self.code_editor.pack(fill="x", pady=5)
        
        self.note_label = ctk.CTkLabel(self.insp_right, text="Note", font=("Arial", 14, "bold"), text_color=self.text_color_primary)
        self.note_label.pack(anchor="w", pady=(10,0))
        self.note_editor = ctk.CTkTextbox(self.insp_right, height=150)
        self.note_editor.pack(fill="x", pady=5)
        
        self.save_note_btn = ctk.CTkButton(self.insp_right, text="Save", command=self.save_current_note)
        self.save_note_btn.pack(fill="x", pady=10)

        # 快捷键提示
        self.arrow_hint_label = ctk.CTkLabel(self.insp_right, text="", text_color="#888888", font=("Arial", 11), wraplength=380, justify="center")
        self.arrow_hint_label.pack(pady=(5, 10))

        self.current_packages_str = ""
        
        # 绑定方向键
        self.bind("<Up>", lambda e: self.navigate_inspector(-1))
        self.bind("<Down>", lambda e: self.navigate_inspector(1))
        
        # === LED 状态栏 ===
        self.status_frame = ctk.CTkFrame(self, height=28, width=420, corner_radius=14, fg_color="#e8ecf1")
        self.status_frame.place(relx=0.5, rely=1.0, anchor="s", y=-6)
        self.status_frame.grid_propagate(False)
        self.status_frame.grid_columnconfigure(1, weight=1)
        
        self.status_dot = ctk.CTkLabel(self.status_frame, text="●", font=("Consolas", 10),
                                       text_color="#c0c5cc", width=16)
        self.status_dot.grid(row=0, column=0, padx=(10, 3), pady=3)
        
        self.status_label = ctk.CTkLabel(self.status_frame, text="Ready",
                                         font=("Consolas", 10), text_color="#a0a5ac",
                                         anchor="w")
        self.status_label.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=3)
        
        self._status_blink_id = None
        self._status_active = False
        self._blink_state = True
        self._led_bg = "#e8ecf1"
        
        self.update_language("CN") # 初始化语言

    def set_status(self, msg, active=True):
        """线程安全的 LED 状态更新"""
        def _update():
            self.status_label.configure(text=msg)
            if active:
                self.status_label.configure(text_color="#2980B9")
                self.status_dot.configure(text_color="#2980B9")
                self._status_active = True
                self._start_blink()
            else:
                self._status_active = False
                if self._status_blink_id:
                    self.after_cancel(self._status_blink_id)
                    self._status_blink_id = None
                if "✅" in msg:
                    self.status_label.configure(text_color="#27ae60")
                    self.status_dot.configure(text_color="#27ae60")
                elif "❌" in msg:
                    self.status_label.configure(text_color="#e74c3c")
                    self.status_dot.configure(text_color="#e74c3c")
                self.after(5000, self._fade_status)
        self.after(0, _update)

    def _start_blink(self):
        if self._status_blink_id:
            self.after_cancel(self._status_blink_id)
        self._blink_state = True
        self._blink_dot()

    def _blink_dot(self):
        if not self._status_active:
            return
        if self._blink_state:
            self.status_dot.configure(text_color="#2980B9")
        else:
            self.status_dot.configure(text_color=self._led_bg)
        self._blink_state = not self._blink_state
        self._status_blink_id = self.after(600, self._blink_dot)

    def _fade_status(self):
        if not self._status_active:
            self.status_label.configure(text_color="#c0c5cc")
            self.status_dot.configure(text_color="#c0c5cc")

    def change_language(self, value):
        self.lang = value
        self.t = TRANSLATIONS[value]
        self.update_language(value)
        self.refresh_library()

    def update_language(self, lang):
        t = TRANSLATIONS[lang]
        self.title(t['title'])
        self.logo_label.configure(text=t['logo_text'])
        self.api_group_label.configure(text=t['api_group'])
        self.api_input.configure(placeholder_text=t['api_key_ph'])
        self.base_url_input.configure(placeholder_text=t['base_url_ph'])
        self.model_input.configure(placeholder_text=t['model_ph'])
        self.path_btn.configure(text=t['path_btn'])
        self.task_group_label.configure(text=t['task_group'])
        self.arxiv_input.configure(placeholder_text=t['arxiv_ph'])
        self.import_local_btn.configure(text=t['import_btn'])
        self.clean_mode_checkbox.configure(text=t['clean_mode'])
        self.clean_hint_label.configure(text=t['clean_hint'])
        self.run_btn.configure(text=t['run_btn'])
        self.copyright_label.configure(text=t['copyright'])
        
        # TabView titles
        try:
            self.tabview._segmented_button._buttons_dict["Library"].configure(text=t['tab_lib'])
            self.tabview._segmented_button._buttons_dict["Inspector"].configure(text=t['tab_insp'])
        except: pass

        self.pkg_label.configure(text=t['pkg_label'])
        self.copy_pkg_btn.configure(text=t['copy_btn'])
        self.src_label.configure(text=t['src_label'])
        self.note_label.configure(text=t['note_label'])
        self.save_note_btn.configure(text=t['save_note_btn'])
        self.arrow_hint_label.configure(text=t['arrow_hint'])

    def update_provider_settings(self, provider):
        defaults = {
            "OpenAI": ("https://api.openai.com/v1", "gpt-3.5-turbo"),
            "Google": ("", "gemini-pro"),
            "DeepSeek": ("https://api.deepseek.com", "deepseek-chat"),
            "Qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus")
        }
        if provider in defaults:
            url, model = defaults[provider]
            self.base_url_input.delete(0, "end")
            self.base_url_input.insert(0, url)
            self.model_input.delete(0, "end")
            self.model_input.insert(0, model)

    def change_path(self):
        new_path = filedialog.askdirectory()
        if new_path:
            self.data_manager.save_config({"storage_path": new_path})
            self.refresh_library()
            return True
        return False

    def start_extract_thread(self, mode="arxiv", data=None):
        threading.Thread(target=self.run_extraction, args=(mode, data), daemon=True).start()

    def import_local(self):
        file_path = filedialog.askopenfilename(filetypes=[("LaTeX Files", "*.tex"), ("All Files", "*.*")])
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                filename = os.path.basename(file_path)
                self.start_extract_thread(mode="local", data={"content": content, "filename": filename})
            except Exception as e:
                messagebox.showerror("Error", f"Read failed: {e}")

    def run_extraction(self, mode="arxiv", data=None):
        # 检查是否设置了存储路径
        if not self.data_manager.config.get("storage_path"):
            self.after(0, lambda: messagebox.showwarning(self.t["title"], self.t["warn_no_path"]))
            # 尝试让用户选择
            self.after(0, self.change_path)
            # 无论选择与否，本次都不继续，让用户重新点击
            return

        api_key = self.api_input.get()
        base_url = self.base_url_input.get()
        provider = self.provider_var.get()
        model = self.model_input.get()
        clean_char = " " if self.clean_char_var.get() == "SPACE" else "-"
        
        if not api_key: 
            self.after(0, lambda: messagebox.showwarning(self.t["title"], self.t["warn_no_api"]))
            return

        if provider != "Google" and not base_url:
             self.after(0, lambda: messagebox.showwarning(self.t["title"], self.t["warn_no_url"]))
             return

        self.data_manager.save_config({
            "api_key": api_key,
            "base_url": base_url,
            "provider": provider,
            "model": model,
            "clean_char": self.clean_char_var.get()
        })

        self.run_btn.configure(state="disabled", text=self.t["run_btn_loading"])
        try:
            if mode == "local":
                source = data["content"]
                doc_id = f"Local_{data['filename']}"
                self.set_status("📂 Loading local file...")
            else:
                doc_id = self.arxiv_input.get()
                if not doc_id: 
                    self.after(0, lambda: messagebox.showwarning("Tip", "ID required"))
                    return 
                self.set_status("📡 Fetching ArXiv source...")
                source = self.logic.fetch_arxiv_source(doc_id)

            self.set_status("🔍 Pre-scanning tables...")
            self.set_status("🤖 LLM extracting tables...")
            tables = self.logic.extract_and_analyze(
                api_key, base_url, source, 
                provider=provider, model=model,
                clean_mode=self.clean_mode_var.get(),
                clean_char=clean_char
            )
            print(f"\n[INFO] LLM 初始提取了 {len(tables)} 个表格")
            self.set_status(f"📋 Found {len(tables)} tables, preparing preamble...")
            # 从原始源码中提取 preamble（宏包+定义）
            src_pkgs, src_defs = self.logic.extract_source_preamble(source)
            
            success_count = 0
            fail_count = 0
            total = len(tables)
            results = []  # 记录每个表格的结果
            # 构建 API 配置用于 LLM 修复
            api_cfg = {
                'api_key': api_key,
                'base_url': base_url,
                'provider': provider,
                'model': model,
            }
            
            for idx, t in enumerate(tables, 1):
                self.set_status(f"⚙️ Compiling table {idx}/{total}...")
                try:
                    img_path, method = self.logic.render_latex(
                        t['code'], src_pkgs, src_defs,
                        api_config=api_cfg, original_source=source,
                        status_cb=lambda msg, i=idx, n=total: self.set_status(f"[{i}/{n}] {msg}")
                    )
                    self.data_manager.add_table(doc_id, t['code'], t.get('packages', []), img_path)
                    try: os.remove(img_path) 
                    except: pass
                    success_count += 1
                    results.append((idx, "✅", method))
                    self.set_status(f"✅ Table {idx}/{total} OK ({method})")
                except Exception as render_err:
                    fail_count += 1
                    results.append((idx, "❌", "FAIL"))
                    self.set_status(f"❌ Table {idx}/{total} failed")
                    print(f"[WARN] Table {idx} failed: {str(render_err)[:200]}")
            
            # 打印清晰的摘要日志
            print(f"\n{'='*50}")
            print(f"  提取摘要: 初始提取 {total} 个表格")
            print(f"{'='*50}")
            for r_idx, r_status, r_method in results:
                print(f"  Table {r_idx:>2}/{total}  {r_status}  {r_method}")
            print(f"{'='*50}")
            print(f"  结果: {success_count} 成功, {fail_count} 失败")
            print(f"{'='*50}\n")
            
            self.after(0, self.refresh_library)
            result_msg = f"✅ Done: {success_count} ok"
            if fail_count > 0:
                result_msg += f", {fail_count} fail"
            self.set_status(result_msg, active=False)
            msg = self.t["success_msg"].format(success_count)
            if fail_count > 0:
                msg += f" ({fail_count} failed)"
            self.after(0, lambda m=msg: messagebox.showinfo(self.t["success_title"], m))
        except Exception as e:
            err_msg = str(e)
            self.set_status("❌ Error", active=False)
            self.after(0, lambda: messagebox.showerror("Error", err_msg))
        finally:
            self.run_btn.configure(state="normal", text=self.t["run_btn"])

    def refresh_library(self):
        for w in self.library_frame.winfo_children(): w.destroy()
        data = self.data_manager.get_all_tables()
        self.library_data = data if data else []
        if not data: return
        
        for row in data:
            tid, aid, code, pkgs, note, img, time = row
            card = ctk.CTkFrame(self.library_frame)
            card.pack(fill="x", pady=5, padx=5)
            
            # Left: ID and Packages
            pkg_count = len(pkgs.split(',')) if pkgs else 0
            title = f"{aid} | {pkg_count} Pkgs"
            ctk.CTkLabel(card, text=title, font=("Arial", 12, "bold"), text_color=self.text_color_primary).pack(side="left", padx=10)
            
            # Middle: Note (Truncated)
            if note:
                note_display = note if len(note) < 30 else note[:30] + "..."
                ctk.CTkLabel(card, text=note_display, text_color="gray", font=("Arial", 11)).pack(side="left", padx=10)

            # Right: Buttons
            ctk.CTkButton(card, text=self.t["lib_view"], width=60, 
                          command=lambda r=row: self.load_detail(r)).pack(side="right", padx=10, pady=10)
            ctk.CTkButton(card, text=self.t["lib_del"], width=50, fg_color="#C0392B", 
                          command=lambda rid=tid: self.delete_item(rid)).pack(side="right", padx=5)

    def load_detail(self, row):
        tid, aid, code, pkgs, note, img_file, time = row
        self.current_table_id = tid
        self.current_packages_str = pkgs
        
        # 更新当前索引
        for i, r in enumerate(self.library_data):
            if r[0] == tid:
                self.current_index = i
                break
        
        self.code_editor.delete("0.0", "end")
        self.code_editor.insert("0.0", code)
        self.note_editor.delete("0.0", "end")
        self.note_editor.insert("0.0", note)

        for w in self.packages_frame.winfo_children(): w.destroy()
        if pkgs:
            r, c = 0, 0
            for pkg in pkgs.split(','):
                btn = ctk.CTkButton(self.packages_frame, text=pkg.strip(), width=60, height=24, fg_color="#2980B9", hover=False)
                btn.grid(row=r, column=c, padx=2, pady=2)
                c += 1
                if c > 3: c, r = 0, r + 1

        full_img_path = os.path.join(self.data_manager.img_dir, img_file)
        if os.path.exists(full_img_path):
            pil_img = Image.open(full_img_path)
            ratio = min(600/pil_img.width, 800/pil_img.height, 1.0)
            ctk_img = ctk.CTkImage(pil_img, size=(int(pil_img.width*ratio), int(pil_img.height*ratio)))
            self.img_preview.configure(image=ctk_img, text="")
        else: self.img_preview.configure(image=None, text=self.t["preview_lost"])
        self.tabview.set("Inspector")

    def navigate_inspector(self, direction):
        """方向键翻页: direction=-1 上一个, direction=1 下一个"""
        if not self.library_data or self.current_index < 0:
            return
        # 仅在 Inspector 标签页激活时生效
        try:
            if self.tabview.get() != "Inspector":
                return
        except: return
        
        new_index = self.current_index + direction
        if 0 <= new_index < len(self.library_data):
            self.load_detail(self.library_data[new_index])

    def copy_packages(self):
        if not self.current_packages_str: return
        cmds = "\n".join([f"\\usepackage{{{p.strip()}}}" for p in self.current_packages_str.split(',')])
        self.clipboard_clear()
        self.clipboard_append(cmds)
        messagebox.showinfo(self.t["copy_success_title"], cmds)

    def save_current_note(self):
        if self.current_table_id:
            self.data_manager.update_note(self.current_table_id, self.note_editor.get("0.0", "end").strip())
            self.refresh_library()

    def delete_item(self, tid):
        if messagebox.askyesno(self.t["title"], self.t["confirm_del"]):
            self.data_manager.delete_table(tid)
            self.refresh_library()

if __name__ == "__main__":
    app = App()
    app.mainloop()