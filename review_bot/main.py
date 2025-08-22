import os, re, html, json, requests
from typing import List, Dict, Tuple
from .llm import review  # if not a package, change to: from llm import review

# ---------- GitHub ----------
def gh(path: str, method="GET", **kw):
    tok = os.getenv("GITHUB_TOKEN")
    if not tok: raise SystemExit("GITHUB_TOKEN missing.")
    hdr = kw.pop("headers", {})
    hdr.update({"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    r = requests.request(method, f"https://api.github.com{path}", headers=hdr, timeout=30, **kw)
    if r.status_code >= 400: raise SystemExit(f"GitHub {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_context() -> Tuple[str, str, int, str]:
    owner, repo = os.getenv("GITHUB_REPOSITORY","").split("/",1)
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f:
        evt = json.load(f)
    pr = evt["pull_request"]
    return owner, repo, pr["number"], pr["head"]["sha"]

# ---------- Confluence ----------
def fetch_confluence_spec() -> str:
    url, user, token = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (url and user and token): raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API.")
    r = requests.get(url, auth=(user, token), timeout=30); r.raise_for_status()
    js = r.json()
    storage = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if storage:
        t = re.sub(r"</p>", "\n", storage, flags=re.I)
        t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", "", t)
        return html.unescape(t).strip()
    return json.dumps(js, indent=2)

# ---------- files / diffs ----------
def fetch_file_content(owner: str, repo: str, path: str, ref: str, raw_url: str | None) -> str:
    if raw_url:
        rr = requests.get(raw_url, timeout=30)
        if rr.ok: return rr.text
    raw = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}", "Accept": "application/vnd.github.raw"},
        timeout=30,
    )
    if raw.ok and raw.text: return raw.text
    j = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"},
        timeout=30,
    )
    if j.ok:
        js = j.json()
        if isinstance(js, dict) and js.get("encoding") == "base64":
            import base64; return base64.b64decode(js.get("content","")).decode("utf-8","ignore")
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
            "content": fetch_file_content(owner, repo, path, head_sha, f.get("raw_url")),
        })
    return out

def candidate_anchor_lines(patch: str) -> List[int]:
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

def post_inline_comment(owner: str, repo: str, pr: int, head_sha: str, path: str, line: int, body_md: str) -> bool:
    try:
        gh(f"/repos/{owner}/{repo}/pulls/{pr}/comments", method="POST",
           json={"body": body_md[:65000], "commit_id": head_sha, "path": path, "side": "RIGHT", "line": int(line)})
        return True
    except SystemExit:
        return False

# ---------- parse LLM output (## file -> ### part) ----------
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
    out = {}; lower = {k.lower(): k for k in secs.keys()}
    for f in files:
        p = f["path"]; key = secs.get(p) or secs.get(lower.get(p.lower(), ""))
        if not key: out[p] = [f"## {p}\n_No model output._"]; continue
        parts, curp = [], []
        for ln in key:
            if PART_H.match(ln.strip()):
                if curp: parts.append(curp); curp = [ln]
            else:
                (curp if curp else []).append(ln)
        if curp: parts.append(curp)
        out[p] = ["\n".join(x).strip() for x in (parts or [key])]
    return out

def map_parts_to_lines(files: List[Dict], per_file_parts: Dict[str, List[str]]) -> Dict[str, List[Tuple[str,int]]]:
    out = {}
    for f in files:
        path = f["path"]; parts = per_file_parts.get(path, []); anchors = candidate_anchor_lines(f.get("patch",""))
        if not anchors: anchors=[1]
        mapped=[]
        for i, md in enumerate(parts):
            if not SUG_RE.search(md or ""):  # only actionable suggestions
                continue
            line = anchors[i] if i < len(anchors) else anchors[-1]
            mapped.append((md, line))
        out[path]=mapped
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

    review_md = review(spec_text, files)
    per_file_parts = split_llm_parts(review_md, files)
    mapped = map_parts_to_lines(files, per_file_parts)

    post_summary_review(owner, repo, pr_number, head_sha,
                        f"### 🤖 Review Bot\n**Spec:** {os.getenv('CONF_URL')}\n\nPosting model suggestions per section.")

    posted = 0
    for path, arr in mapped.items():
        for body_md, line in arr:
            if post_inline_comment(owner, repo, pr_number, head_sha, path, line, body_md):
                posted += 1

    if posted == 0:
        body = ["### 🤖 Review Bot (summary only)"]
        for p, parts in per_file_parts.items():
            body.append(f"## {p}\n" + "\n\n---\n\n".join(parts))
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST",
           json={"body": "\n\n".join(body)[:65000]})

if __name__ == "__main__":
    main()