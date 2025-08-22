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
    r = requests.request(method, f"https://api.github.com{path}", headers=hdr, timeout=30, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"GitHub {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_context() -> Tuple[str, str, int, str]:
    owner, repo = os.getenv("GITHUB_REPOSITORY", "").split("/", 1)
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f:
        evt = json.load(f)
    pr = evt["pull_request"]
    return owner, repo, pr["number"], pr["head"]["sha"]

# ---------- Confluence ----------
def fetch_confluence_spec() -> str:
    url, user, token = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (url and user and token):
        raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API.")
    r = requests.get(url, auth=(user, token), timeout=30); r.raise_for_status()
    js = r.json()
    storage = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if storage:
        t = re.sub(r"</p>", "\n", storage, flags=re.I); t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I); t = re.sub(r"<[^>]+>", "", t)
        return html.unescape(t).strip()
    return json.dumps(js, indent=2)

# ---------- file/diff retrieval ----------
def fetch_file_content(owner: str, repo: str, path: str, ref: str, raw_url: Optional[str]) -> str:
    if raw_url:
        rr = requests.get(raw_url, timeout=30)
        if rr.ok: return rr.text
    try:
        raw = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
            headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}", "Accept": "application/vnd.github.raw"},
            timeout=30,
        )
        if raw.ok and raw.text: return raw.text
    except Exception:
        pass
    cj = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"},
        timeout=30,
    )
    if cj.ok:
        js = cj.json()
        if isinstance(js, dict) and js.get("encoding") == "base64":
            import base64; return base64.b64decode(js.get("content", "")).decode("utf-8", errors="ignore")
    return ""

def list_changed_files(owner: str, repo: str, pr_number: int, head_sha: str) -> List[Dict]:
    files = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    out = []
    for f in files:
        if not f["filename"].endswith(".py"): continue
        path = f["filename"]
        out.append({
            "path": path,
            "patch": f.get("patch") or "",
            "raw_url": f.get("raw_url"),
            "status": f.get("status"),
            "content": fetch_file_content(owner, repo, path, head_sha, f.get("raw_url")),
        })
    return out

def diff_anchor_lines(patch: str) -> List[int]:
    if not patch: return [1]
    adds, ctx, head = [], [], 1
    for ln in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m: head = int(m.group(1)); continue
        if ln.startswith('+') and not ln.startswith('+++'): adds.append(head); head += 1; continue
        if ln.startswith(' ') or (ln and ln[0] not in '+-@'): ctx.append(head); head += 1; continue
        if ln.startswith('-') and not ln.startswith('---'): continue
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
        payload["start_line"] = int(start_line); payload["start_side"] = "RIGHT"
    try:
        gh(f"/repos/{owner}/{repo}/pulls/{pr}/comments", method="POST", json=payload); return True
    except SystemExit: return False

# ---------- LLM output parsing ----------
FILE_H = re.compile(r"^##\s+(.+\.py)\s*$"); PART_H = re.compile(r"^###\s+(.+)\s*$"); SUG_RE = re.compile(r"```suggestion\b", re.I)

def split_llm_parts(md: str, files: List[Dict]) -> Dict[str, List[str]]:
    secs, cur, buf = {}, None, []
    for line in md.splitlines():
        m = FILE_H.match(line.strip())
        if m:
            if cur and buf: secs[cur] = buf[:]
            cur = m.group(1).strip(); buf = [line]
        elif cur: buf.append(line)
    if cur and buf: secs[cur] = buf[:]
    out = {}
    lower = {k.lower(): k for k in secs.keys()}
    for f in files:
        p = f["path"]; key = secs.get(p) or secs.get(lower.get(p.lower(), ""))
        if not key: out[p] = [f"## {p}\n_No explicit suggestions from model._"]; continue
        parts, curp = [], []
        for ln in key:
            if PART_H.match(ln.strip()):
                if curp: parts.append(curp); curp = [ln]
            else:
                (curp if curp else []).append(ln)
        if curp: parts.append(curp)
        out[p] = ["\n".join(x).strip() for x in (parts or [key])]
    return out

# ---------- source sections & static checks ----------
SECTION_TAG = re.compile(r"^\s*#\s*---\s*section:\s*(.+?)\s*---\s*$", re.I)

def extract_source_sections(text: str) -> List[Tuple[str, int, int]]:
    lines = text.splitlines(); marks = []
    for i, line in enumerate(lines, 1):
        m = SECTION_TAG.match(line)
        if m: marks.append((m.group(1).strip(), i))
    spans = []
    for idx, (title, start) in enumerate(marks):
        end = (marks[idx + 1][1] - 1) if idx + 1 < len(marks) else len(lines)
        spans.append((title, start, end))
    if not spans: spans.append(("entire-file", 1, len(lines)))
    return spans

def static_suggestions_for_section(path: str, text: List[str], start: int) -> List[Tuple[str, int, Optional[int]]]:
    out: List[Tuple[str, int, Optional[int]]] = []
    for idx, line in enumerate(text, start):
        if re.search(r"\beval\s*\(", line):
            md = (f"## {path}\n### Replace unsafe eval() with ast.literal_eval\n"
                  "```suggestion\ntry:\n    import ast\n    return ast.literal_eval(src)\n"
                  "except (ValueError, SyntaxError):\n    return {}\n```\n")
            out.append((md, idx, idx))
        if "yaml.load" in line and "SafeLoader" not in "".join(text):
            out.append((f"## {path}\n### Use yaml.safe_load\n```suggestion\nimport yaml\nreturn yaml.safe_load(text)\n```\n", idx, None))
        if re.search(r"except\s+Exception\s*:", line):
            out.append((f"## {path}\n### Catch specific exceptions\n```suggestion\nexcept FileNotFoundError:\n    return \n```\n", idx, None))
        if re.search(r"\bopen\s*\(", line) and not any("with open" in l for l in text):
            out.append((f"## {path}\n### Use a context manager\n```suggestion\nwith open(path,'r',encoding='utf-8') as f:\n    data=f.read()\n```\n", idx, None))
    return out

def ensure_actionable_parts(
    files: List[Dict],
    per_file_from_llm: Dict[str, List[str]],
    patch_by_path: Dict[str, str],
) -> Dict[str, List[Tuple[str, int, Optional[int]]]]:
    out: Dict[str, List[Tuple[str, int, Optional[int]]]] = {}
    for f in files:
        path, text = f["path"], (f.get("content", "") or "")
        lines = text.splitlines()
        anchors = diff_anchor_lines(patch_by_path.get(path, ""))
        anchor_set = set(anchors)
        parts: List[Tuple[str, int, Optional[int]]] = []

        for part in per_file_from_llm.get(path, []):
            line = anchors[0] if anchors else 1
            if not SUG_RE.search(part or ""):
                cur = lines[line-1] if 1 <= line <= len(lines) else ""
                nl = (cur if cur.strip() else "#")
                if "review-bot" not in nl:
                    nl += ("  # review-bot" if nl.strip() and not nl.strip().startswith("#") else " (review-bot)")
                part = part + "\n\n```suggestion\n" + nl + "\n```"
            parts.append((part, line, None))

        for _, s, e in extract_source_sections(text):
            sect = lines[s-1:e]
            for md, sug_line, start_line in static_suggestions_for_section(path, sect, s):
                if sug_line in anchor_set:
                    parts.append((md, sug_line, start_line))
                else:
                    near = min(anchors, key=lambda a: abs(a - sug_line)) if anchors else 1
                    parts.append((md, near, None))

        if not parts:
            line = anchors[0] if anchors else 1
            cur = lines[line-1] if 1 <= line <= len(lines) else ""
            nl = (cur if cur.strip() else "#")
            if "review-bot" not in nl:
                nl += ("  # review-bot" if nl.strip() and not nl.strip().startswith("#") else " (review-bot)")
            parts = [(f"## {path}\n_No issues detected._\n\n```suggestion\n{nl}\n```", line, None)]

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
    meta = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    patch_by_path = {m["filename"]: (m.get("patch") or "") for m in meta}

    review_md = review(spec_text, files)
    llm_parts = split_llm_parts(review_md, files)
    actionable = ensure_actionable_parts(files, llm_parts, patch_by_path)

    post_summary_review(owner, repo, pr_number, head_sha,
                        f"### 🤖 Review Bot\n**Spec:** {os.getenv('CONF_URL')}\n\nActionable suggestions are posted per section.")
    posted = 0
    for p, arr in actionable.items():
        for body_md, line, start_line in arr:
            if post_inline_comment(owner, repo, pr_number, head_sha, p, line, body_md, start_line=start_line):
                posted += 1
    if posted == 0:
        body = ["### 🤖 Review Bot (fallback)"]
        for p, arr in actionable.items():
            body.append(f"## {p}\n" + "\n\n---\n\n".join(md for md, _, _ in arr))
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST",
           json={"body": "\n\n".join(body)[:65000]})

if __name__ == "__main__":
    main()
