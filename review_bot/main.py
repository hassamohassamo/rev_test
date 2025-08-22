# review_bot/main.py
import os, re, html, json, base64, requests
from typing import List, Dict, Tuple, Optional
from .llm import review, make_llm  # review() for whole-PR pass, make_llm() for per-section fallback

# ---------- tiny GitHub helpers ----------
def gh(path: str, method: str = "GET", **kw):
    tok = os.getenv("GITHUB_TOKEN");  assert tok, "GITHUB_TOKEN missing."
    hdr = kw.pop("headers", {});  hdr.update({"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    r = requests.request(method, f"https://api.github.com{path}", headers=hdr, timeout=30, **kw)
    if r.status_code >= 400: raise SystemExit(f"GitHub {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_ctx() -> Tuple[str, str, int, str]:
    owner, repo = os.getenv("GITHUB_REPOSITORY","").split("/",1)
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f: evt = json.load(f)
    pr = evt["pull_request"];  return owner, repo, pr["number"], pr["head"]["sha"]

# ---------- Confluence ----------
def fetch_spec() -> str:
    url, user, token = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (url and user and token): raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API.")
    r = requests.get(url, auth=(user, token), timeout=30); r.raise_for_status()
    js = r.json(); html_body = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if not html_body: return json.dumps(js, indent=2)
    t = re.sub(r"</p>", "\n", html_body, flags=re.I); t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I); t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()

# ---------- files / diffs ----------
def fetch_content(owner: str, repo: str, path: str, ref: str, raw_url: Optional[str]) -> str:
    if raw_url:
        rr = requests.get(raw_url, timeout=30)
        if rr.ok: return rr.text
    r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
                     headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}", "Accept": "application/vnd.github.raw"},
                     timeout=30)
    if r.ok and r.text: return r.text
    r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
                     headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"},
                     timeout=30)
    if r.ok:
        js = r.json()
        if isinstance(js, dict) and js.get("encoding") == "base64":
            return base64.b64decode(js.get("content","")).decode("utf-8","ignore")
    return ""

def changed_py_files(owner: str, repo: str, pr_number: int, head_sha: str) -> List[Dict]:
    files = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    out = []
    for f in files:
        if f["filename"].endswith(".py"):
            p = f["filename"]
            out.append({"path": p,
                        "patch": f.get("patch") or "",
                        "raw_url": f.get("raw_url"),
                        "status": f.get("status",""),
                        "content": fetch_content(owner, repo, p, head_sha, f.get("raw_url"))})
    return out

def anchor_lines(patch: str) -> List[int]:
    if not patch: return [1]
    head=1; add, ctx = [], []
    for ln in patch.splitlines():
        m=re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m: head=int(m.group(1)); continue
        if ln.startswith('+') and not ln.startswith('+++'): add.append(head); head+=1; continue
        if ln.startswith(' ') or (ln and ln[0] not in '+-@'): ctx.append(head); head+=1; continue
        if ln.startswith('-') and not ln.startswith('---'): continue
    return (add+ctx) or [1]

# ---------- sections ----------
SEC = re.compile(r"^\s*#\s*---\s*section:\s*(.+?)\s*---\s*$", re.I)
def find_sections(text: str) -> List[Tuple[str,int,int]]:
    lines = text.splitlines(); marks=[]
    for i,l in enumerate(lines,1):
        m=SEC.match(l);  
        if m: marks.append((m.group(1).strip(), i))
    if not marks: return [("entire-file",1,len(lines))]
    spans=[]; 
    for i,(title,start) in enumerate(marks):
        end = (marks[i+1][1]-1) if i+1<len(marks) else len(lines)
        spans.append((title,start,end))
    return spans

# ---------- posting ----------
def post_summary(owner:str, repo:str, pr:int, sha:str, body:str):
    gh(f"/repos/{owner}/{repo}/pulls/{pr}/reviews", method="POST",
       json={"commit_id": sha, "event": "COMMENT", "body": body[:65000]})

def post_suggestion(owner:str, repo:str, pr:int, sha:str, path:str, start_line:int, end_line:int, body:str) -> bool:
    try:
        gh(f"/repos/{owner}/{repo}/pulls/{pr}/comments", method="POST",
           json={"commit_id": sha, "path": path, "side": "RIGHT",
                 "start_line": int(start_line), "start_side":"RIGHT",
                 "line": int(end_line), "body": body[:65000]})
        return True
    except SystemExit as e:
        print(f"[bot] failed to post suggestion for {path} {start_line}-{end_line}: {e}")
        return False

# ---------- LLM helpers ----------
HAS_SUG = re.compile(r"```suggestion\b", re.I)

def force_section_fix(llm, spec: str, file_path: str, title: str, section_src: str, diff: str) -> str:
    """Ask the model for one actionable fix for this section; must output a GitHub suggestion block."""
    prompt = (
        "You are reviewing ONE code *section* in a pull request.\n"
        "SPEC (must follow):\n"
        f"{spec[:10000]}\n\n"
        f"FILE: {file_path}\nSECTION: {title}\n\n"
        "Unified diff for context (may be partial):\n"
        f"```diff\n{diff[:8000]}\n```\n\n"
        "Section source to review:\n"
        f"```python\n{section_src[:8000]}\n```\n\n"
        "TASK: If changes are needed, output exactly ONE GitHub suggestion block that replaces this **section**.\n"
        "If no change is needed, reply with '# ok' only.\n"
        "Format ONLY as:\n"
        "```suggestion\n<full improved section code>\n```\n"
    )
    resp = llm.invoke([
        {"role":"system","content":"Be precise. Respect the SPEC. Output a single suggestion block or '# ok'."},
        {"role":"user","content":prompt}
    ])
    return getattr(resp, "content", str(resp))

# ---------- main ----------
def main():
    owner, repo, pr, sha = pr_ctx()
    spec = fetch_spec()
    files = changed_py_files(owner, repo, pr, sha)
    if not files:
        gh(f"/repos/{owner}/{repo}/issues/{pr}/comments", method="POST",
           json={"body":"🤖 Review Bot: No Python file changes detected."})
        return

    # Pass 1: whole-PR review (kept for richer guidance in the Checks tab)
    whole_md = review(spec, files) or ""
    post_summary(owner, repo, pr, sha,
                 f"### 🤖 Review Bot\n**Spec:** {os.getenv('CONF_URL')}\n\n"
                 "Inline suggestions are posted per section. Full review is available in this check.\n\n"
                 f"{whole_md[:45000]}")

    # Pass 2 (guaranteed actionable): per-section fix requests
    llm = make_llm()
    posted = 0
    for f in files:
        path, text, patch = f["path"], (f.get("content") or ""), f.get("patch","")
        if not text: continue
        anchors = anchor_lines(patch)
        for title, start, end in find_sections(text):
            # Only post inline if the section touches the diff (GitHub requires diff lines)
            diff_lines_in_section = [a for a in anchors if start <= a <= end]
            if not diff_lines_in_section:
                continue
            section_src = "\n".join(text.splitlines()[start-1:end])
            md = force_section_fix(llm, spec, path, title, section_src, patch)
            if md.strip() == "# ok":  # model thinks it's fine
                continue
            if not HAS_SUG.search(md or ""):
                continue  # do not fabricate fixes; rely on LLM
            # anchor to the last touched line in this section (multi-line suggestion uses start_line + line)
            end_line = diff_lines_in_section[-1]
            if post_suggestion(owner, repo, pr, sha, path, start_line=start, end_line=end_line, body=md):
                posted += 1

    if posted == 0:
        gh(f"/repos/{owner}/{repo}/issues/{pr}/comments", method="POST",
           json={"body":"🤖 Review Bot: No inline suggestions could be anchored to the diff (sections unchanged). "
                        "See the review summary above for guidance."})

if __name__ == "__main__":
    main()
