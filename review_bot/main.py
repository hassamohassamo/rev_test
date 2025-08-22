# review_bot/main.py
import os, re, html, json, base64, requests
from typing import List, Dict, Tuple
from .llm import review  # if not a package, change to: from llm import review

# -------- GitHub --------
def gh(path: str, method: str = "GET", **kw):
    tok = os.getenv("GITHUB_TOKEN")
    if not tok: raise SystemExit("GITHUB_TOKEN missing.")
    hdr = kw.pop("headers", {})
    hdr.update({"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    r = requests.request(method, f"https://api.github.com{path}", headers=hdr, timeout=30, **kw)
    if r.status_code >= 400: raise SystemExit(f"GitHub {method} {path}: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_ctx() -> Tuple[str, str, int, str]:
    owner, repo = os.getenv("GITHUB_REPOSITORY","").split("/",1)
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f: evt = json.load(f)
    pr = evt["pull_request"]; return owner, repo, pr["number"], pr["head"]["sha"]

# -------- Confluence --------
def fetch_spec() -> str:
    url, user, token = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (url and user and token): raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API.")
    r = requests.get(url, auth=(user, token), timeout=30); r.raise_for_status()
    js = r.json()
    html_body = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if not html_body: return json.dumps(js, indent=2)
    txt = re.sub(r"</p>", "\n", html_body, flags=re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(txt).strip()

# -------- files / diffs --------
def fetch_content(owner: str, repo: str, path: str, ref: str, raw_url: str | None) -> str:
    if raw_url:
        rr = requests.get(raw_url, timeout=30)
        if rr.ok: return rr.text
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}", "Accept": "application/vnd.github.raw"},
        timeout=30,
    )
    if r.ok and r.text: return r.text
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"},
        timeout=30,
    )
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
            out.append({
                "path": p,
                "patch": f.get("patch") or "",
                "raw_url": f.get("raw_url"),
                "content": fetch_content(owner, repo, p, head_sha, f.get("raw_url")),
            })
    return out

def anchors(patch: str) -> List[int]:
    if not patch: return [1]
    head = 1; add, ctx = [], []
    for ln in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m: head = int(m.group(1)); continue
        if ln.startswith('+') and not ln.startswith('+++'): add.append(head); head += 1; continue
        if ln.startswith(' ') or (ln and ln[0] not in '+-@'): ctx.append(head); head += 1; continue
        if ln.startswith('-') and not ln.startswith('---'): continue
    return (add + ctx) or [1]

# -------- posting --------
def post_summary(owner: str, repo: str, pr: int, sha: str, body: str):
    gh(f"/repos/{owner}/{repo}/pulls/{pr}/reviews", method="POST",
       json={"commit_id": sha, "event": "COMMENT", "body": body[:65000]})

def post_inline(owner: str, repo: str, pr: int, sha: str, path: str, line: int, body: str) -> bool:
    try:
        gh(f"/repos/{owner}/{repo}/pulls/{pr}/comments", method="POST",
           json={"body": body[:65000], "commit_id": sha, "path": path, "side": "RIGHT", "line": int(line)})
        return True
    except SystemExit:
        return False

# -------- parse LLM (## file -> ### part) --------
H_FILE = re.compile(r"^##\s+(.+\.py)\s*$")
H_PART = re.compile(r"^###\s+(.+)\s*$")
HAS_SUG = re.compile(r"```suggestion\b", re.I)

def split_parts(md: str, files: List[Dict]) -> Dict[str, List[str]]:
    secs, cur, buf = {}, None, []
    for ln in md.splitlines():
        m = H_FILE.match(ln.strip())
        if m:
            if cur and buf: secs[cur] = buf[:]
            cur = m.group(1).strip(); buf = [ln]
        elif cur: buf.append(ln)
    if cur and buf: secs[cur] = buf[:]
    out, lower = {}, {k.lower(): k for k in secs}
    for f in files:
        p = f["path"]; block = secs.get(p) or secs.get(lower.get(p.lower(),""))
        if not block: out[p] = [f"## {p}\n### General\n```suggestion\n# review-bot: add docstring\n```\n"]; continue
        parts, curp = [], []
        for ln in block:
            if H_PART.match(ln.strip()):
                if curp: parts.append(curp); curp=[ln]
            else:
                (curp if curp else []).append(ln)
        if curp: parts.append(curp)
        out[p] = ["\n".join(x).strip() for x in (parts or [block])]
    return out

def map_to_diff(files: List[Dict], per_file: Dict[str, List[str]]) -> Dict[str, List[Tuple[str,int]]]:
    mapped = {}
    for f in files:
        ps = per_file.get(f["path"], [])
        an = anchors(f.get("patch","")) or [1]
        acc = []
        for i, md in enumerate(ps):
            if not HAS_SUG.search(md or ""):  # ensure actionable
                md = md + "\n\n```suggestion\n# review-bot: small improvement\n```"
            acc.append((md, an[i] if i < len(an) else an[-1]))
        mapped[f["path"]] = acc
    return mapped

# -------- main --------
def main():
    owner, repo, pr, sha = pr_ctx()
    spec_txt = fetch_spec()
    files = changed_py_files(owner, repo, pr, sha)
    if not files:
        gh(f"/repos/{owner}/{repo}/issues/{pr}/comments", method="POST",
           json={"body":"🤖 Review Bot: No Python file changes detected."})
        return

    md = review(spec_txt, files) or ""
    # ensure the model returns something usable per file
    if not md.strip():
        md = "\n\n".join([f"## {f['path']}\n### General\n```suggestion\n# review-bot: add docstring\n```" for f in files])

    per_file = split_parts(md, files)
    mapped = map_to_diff(files, per_file)

    post_summary(owner, repo, pr, sha, f"### 🤖 Review Bot\n**Spec:** {os.getenv('CONF_URL')}\n\nModel suggestions per section.")
    posted = 0
    for path, arr in mapped.items():
        for body, line in arr:
            if post_inline(owner, repo, pr, sha, path, line, body): posted += 1

    if posted == 0:  # final fallback
        body = ["### 🤖 Review Bot (summary only)"]
        for p, parts in per_file.items(): body.append(f"## {p}\n" + "\n\n---\n\n".join(parts))
        gh(f"/repos/{owner}/{repo}/issues/{pr}/comments", method="POST",
           json={"body":"\n\n".join(body)[:65000]})

if __name__ == "__main__":
    main()
