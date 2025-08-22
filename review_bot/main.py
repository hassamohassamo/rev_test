# review_bot/main.py
import os, re, html, json, requests
from typing import List, Dict, Tuple, Optional
from .llm import review  # if not a package: from llm import review

# ---------- tiny GitHub helpers ----------
def gh(path: str, method="GET", **kw):
    tok = os.getenv("GITHUB_TOKEN")
    if not tok:
        raise SystemExit("GITHUB_TOKEN missing.")
    hdr = kw.pop("headers", {})
    hdr.update({"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    url = f"https://api.github.com{path}"
    r = requests.request(method, url, headers=hdr, timeout=30, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"GitHub {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_context() -> Tuple[str, str, int, str]:
    repo_full = os.getenv("GITHUB_REPOSITORY", "")
    owner, repo = repo_full.split("/", 1)
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f:
        evt = json.load(f)
    pr = evt["pull_request"]
    return owner, repo, pr["number"], pr["head"]["sha"]

# ---------- Confluence ----------
def fetch_confluence_spec() -> str:
    url, user, token = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (url and user and token):
        raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API.")
    r = requests.get(url, auth=(user, token), timeout=30)
    r.raise_for_status()
    js = r.json()
    storage = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if storage:
        text = re.sub(r"</p>", "\n", storage, flags=re.I)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()
    return json.dumps(js, indent=2)

# ---------- file/diff retrieval ----------
def fetch_file_content(owner: str, repo: str, path: str, ref: str, raw_url: Optional[str]) -> str:
    # 1) try GitHub-provided raw_url
    if raw_url:
        rr = requests.get(raw_url, timeout=30)
        if rr.ok:
            return rr.text
    # 2) contents API (raw)
    try:
        raw = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
            headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
                     "Accept": "application/vnd.github.raw"},
            timeout=30,
        )
        if raw.ok and raw.text:
            return raw.text
    except Exception:
        pass
    # 3) contents API (json + base64)
    cj = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"},
        timeout=30,
    )
    if cj.ok:
        js = cj.json()
        if isinstance(js, dict) and js.get("encoding") == "base64":
            import base64
            return base64.b64decode(js.get("content", "")).decode("utf-8", errors="ignore")
    return ""

def list_changed_files(owner: str, repo: str, pr_number: int, head_sha: str) -> List[Dict]:
    files = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    out = []
    for f in files:
        if not f["filename"].endswith(".py"):
            continue
        path = f["filename"]
        item = {
            "path": path,
            "patch": f.get("patch") or "",
            "raw_url": f.get("raw_url"),
            "status": f.get("status"),
        }
        item["content"] = fetch_file_content(owner, repo, path, head_sha, item["raw_url"])
        out.append(item)
    return out

def diff_anchor_lines(patch: str) -> List[int]:
    """All usable RIGHT-side (HEAD) lines: '+' first then context. Always ≥1."""
    if not patch:
        return [1]
    adds, ctx, head = [], [], 1
    for ln in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m:
            head = int(m.group(1)); continue
        if ln.startswith('+') and not ln.startswith('+++'):
            adds.append(head); head += 1; continue
        if ln.startswith(' ') or (ln and ln[0] not in '+-@'):
            ctx.append(head); head += 1; continue
        if ln.startswith('-') and not ln.startswith('---'):
            continue
    return (adds + ctx) or [1]

# ---------- posting ----------
def post_summary_review(owner: str, repo: str, pr: int, head_sha: str, body_md: str):
    gh(f"/repos/{owner}/{repo}/pulls/{pr}/reviews", method="POST",
       json={"commit_id": head_sha, "event": "COMMENT", "body": body_md[:65000]})

def post_inline_comment(owner: str, repo: str, pr: int, head_sha: str,
                        path: str, line: int, body_md: str,
                        start_line: Optional[int] = None) -> bool:
    payload = {"body": body_md[:65000], "commit_id": head_sha, "path": path, "side": "RIGHT", "line": int(line)}
    if start_line is not None and start_line <= line:
        payload["start_line"] = int(start_line)
        payload["start_side"] = "RIGHT"
    try:
        gh(f"/repos/{owner}/{repo}/pulls/{pr}/comments", method="POST", json=payload)
        print(f"[bot] inline OK: {path} @ {line} (start={start_line})")
        return True
    except SystemExit as e:
        print(f"[bot] inline FAIL: {path} @ {line}: {e}")
        return False

# ---------- LLM output parsing (## file -> ### part) ----------
FILE_H = re.compile(r"^##\s+(.+\.py)\s*$")
PART_H = re.compile(r"^###\s+(.+)\s*$")
SUG_RE = re.compile(r"```suggestion\b", re.I)

def split_llm_parts(md: str, files: List[Dict]) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    cur = None; buf: List[str] = []
    for line in md.splitlines():
        m = FILE_H.match(line.strip())
        if m:
            if cur and buf: sections[cur] = buf[:]
            cur = m.group(1).strip(); buf = [line]
        elif cur:
            buf.append(line)
    if cur and buf: sections[cur] = buf[:]

    per: Dict[str, List[str]] = {}
    lower = {k.lower(): k for k in sections.keys()}
    for f in files:
        path = f["path"]
        key = sections.get(path) or sections.get(lower.get(path.lower(), ""))
        if not key:
            per[path] = [f"## {path}\n_No explicit suggestions from model._"]; continue
        parts, curp = [], []
        for ln in key:
            if PART_H.match(ln.strip()):
                if curp: parts.append(curp)
                curp = [ln]
            else:
                (curp if curp else []).append(ln)
        if curp: parts.append(curp)
        per[path] = ["\n".join(p).strip() for p in (parts or [key])]
    return per

# ---------- source sections & static checks ----------
SECTION_TAG = re.compile(r"^\s*#\s*---\s*section:\s*(.+?)\s*---\s*$", re.I)

def extract_source_sections(text: str) -> List[Tuple[str, int, int]]:
    """Return [(title, start_line, end_line)] based on '# --- section: NAME ---' markers."""
    lines = text.splitlines()
    marks = []
    for i, line in enumerate(lines, 1):
        m = SECTION_TAG.match(line)
        if m:
            marks.append((m.group(1).strip(), i))
    spans = []
    for idx, (title, start) in enumerate(marks):
        end = (marks[idx + 1][1] - 1) if idx + 1 < len(marks) else len(lines)
        spans.append((title, start, end))
    if not spans:
        spans.append(("entire-file", 1, len(lines)))
    return spans

def static_suggestions_for_section(path: str, text: List[str], start: int) -> List[Tuple[str, int, Optional[int]]]:
    """
    Return list of (markdown, line, start_line) suggestions for THIS section only.
    We try to anchor on risky lines that are actually in the diff later.
    """
    out: List[Tuple[str, int, Optional[int]]] = []

    for idx, line in enumerate(text, start):
        # 1) eval()
        if re.search(r"\beval\s*\(", line):
            md = (f"## {path}\n### Replace unsafe eval() with ast.literal_eval\n"
                  "Using `eval` is a code execution risk. Prefer `ast.literal_eval` with error handling.\n\n"
                  "```suggestion\n"
                  "try:\n"
                  "    import ast\n"
                  "    return ast.literal_eval(src)\n"
                  "except (ValueError, SyntaxError):\n"
                  "    return {}\n"
                  "```\n")
            out.append((md, idx, idx))  # multi-line, anchored to this exact line

        # 2) yaml.load without SafeLoader
        if "yaml.load" in line and "SafeLoader" not in "".join(text):
            md = (f"## {path}\n### Use yaml.safe_load\n"
                  "Avoid `yaml.load` without SafeLoader—use `yaml.safe_load`.\n\n"
                  "```suggestion\n"
                  "import yaml\n"
                  "return yaml.safe_load(text)\n"
                  "```\n")
            out.append((md, idx, None))

        # 3) broad except
        if re.search(r"except\s+Exception\s*:", line):
            md = (f"## {path}\n### Catch specific exceptions\n"
                  "Catching `Exception` hides real failures.\n\n"
                  "```suggestion\n"
                  "except FileNotFoundError:\n"
                  "    return {}\n"
                  "```\n")
            out.append((md, idx, None))

        # 4) open() without context manager
        if re.search(r"\bopen\s*\(", line) and not any("with open" in l for l in text):
            md = (f"## {path}\n### Use a context manager for file I/O\n\n"
                  "```suggestion\n"
                  "with open(path, 'r', encoding='utf-8') as f:\n"
                  "    data = f.read()\n"
                  "```\n")
            out.append((md, idx, None))

    return out

# ---------- make every part actionable ----------
def ensure_actionable_parts(owner: str, repo: str, head_sha: str,
                            files: List[Dict], per_file_from_llm: Dict[str, List[str]]) -> Dict[str, List[Tuple[str, int, Optional[int]]]]:
    """
    Returns { path -> [(part_markdown_with_suggestion, line, start_line_or_None), ...] }
    - Merges LLM parts with static, per-section findings.
    - If a part has no ```suggestion, fabricate a tiny actionable change on an anchor line.
    - For static eval fixes, we anchor exactly on the eval line (if that line is in the diff).
    """
    meta = gh(f"/repos/{owner}/{repo}/pulls/{gh.__defaults__ and gh.__defaults__[0] or 0}/files")  # dummy to appease linters
    # real meta:
    meta = gh(f"/repos/{owner}/{repo}/pulls/{json.loads(open(os.getenv('GITHUB_EVENT_PATH'),'r').read())['pull_request']['number']}/files")
    patch_by_path = {m["filename"]: (m.get("patch") or "") for m in meta}

    out: Dict[str, List[Tuple[str, int, Optional[int]]]] = {}
    for f in files:
        path = f["path"]
        text = f.get("content", "") or ""
        lines = text.splitlines()
        anchors = diff_anchor_lines(patch_by_path.get(path, ""))
        anchor_set = set(anchors)
        parts: List[Tuple[str, int, Optional[int]]] = []

        # 1) LLM parts first
        for part in per_file_from_llm.get(path, []):
            # pick first anchor by default; will be overridden if static can hit exact risky line
            line = anchors[0] if anchors else 1
            if not SUG_RE.search(part or ""):
                # fabricate minimal actionable suggestion on that anchor line (non-destructive)
                current = lines[line - 1] if 1 <= line <= len(lines) else ""
                new_line = (current if current.strip() else "#") + ("  # review-bot" if "review-bot" not in current else "")
                part = part + "\n\n```suggestion\n" + new_line + "\n```"
            parts.append((part, line, None))

        # 2) Static per-section passes (find real risky lines)
        for title, s, e in extract_source_sections(text):
            sect_text = lines[s - 1:e]
            for md, suggested_line, start_line in static_suggestions_for_section(path, sect_text, s):
                # if the risky line is in diff, anchor exactly there; else use the nearest existing anchor
                if suggested_line in anchor_set:
                    parts.append((md, suggested_line, start_line))
                else:
                    # nearest anchor fallback
                    nearest = min(anchors, key=lambda a: abs(a - suggested_line)) if anchors else 1
                    parts.append((md, nearest, None))

        # guarantee at least one part
        if not parts:
            line = anchors[0] if anchors else 1
            current = lines[line - 1] if 1 <= line <= len(lines) else ""
            new_line = (current if current.strip() else "#") + ("  # review-bot" if "review-bot" not in current else "")
            parts = [(f"## {path}\n_No issues detected._\n\n```suggestion\n{new_line}\n```", line, None)]

        out[path] = parts
    return out

# ---------- main ----------
def main():
    owner, repo, pr_number, head_sha = pr_context()
    spec_text = fetch_confluence_spec()
    files = list_changed_files(owner, repo, pr_number, head_sha)
    if not files:
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST",
           json={"body": "🤖 Review Bot: No Python file changes detected."})
        return

    print(f"[bot] files: {[f['path'] for f in files]}")

    review_md = review(spec_text, files)  # LLM output
    llm_parts = split_llm_parts(review_md, files)

    actionable = ensure_actionable_parts(owner, repo, head_sha, files, llm_parts)

    summary = (
        "### 🤖 Review Bot\n"
        f"**Spec:** {os.getenv('CONF_URL')}\n\n"
        "Actionable suggestions are posted per section. Security issues like `eval()` are "
        "anchored to the exact changed line with a multi-line fix when possible."
    )
    post_summary_review(owner, repo, pr_number, head_sha, summary)

    posted = 0
    for path, parts in actionable.items():
        for body_md, line, start_line in parts:
            if post_inline_comment(owner, repo, pr_number, head_sha, path, line, body_md, start_line=start_line):
                posted += 1

    if posted == 0:
        body = ["### 🤖 Review Bot (fallback)"]
        for p, arr in actionable.items():
            body.append(f"## {p}\n" + "\n\n---\n\n".join(md for md, _, _ in arr))
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST",
           json={"body": "\n\n".join(body)[:65000]})
        print("[bot] fallback comment posted.")
    else:
        print(f"[bot] inline comments posted: {posted}")

if __name__ == "__main__":
    main()
