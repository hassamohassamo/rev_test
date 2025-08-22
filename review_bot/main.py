# main.py
import os, re, html, json, requests
from typing import List, Dict, Tuple
from rich import print
from review_bot.llm import review  # or: from .llm import review

# ---------- Confluence ----------
def fetch_confluence_spec() -> str:
    url   = os.getenv("CONF_URL")
    user  = os.getenv("CONF_USERNAME")
    token = os.getenv("CONF_API")
    if not (url and user and token):
        raise SystemExit("Missing CONF_URL / CONF_USERNAME / CONF_API in env.")
    r = requests.get(url, auth=(user, token), timeout=30)
    r.raise_for_status()
    js = r.json()
    storage = (((js or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if storage:
        # quick HTML -> text
        text = re.sub(r"</p>", "\n", storage, flags=re.I)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()
    return json.dumps(js, indent=2)

# ---------- GitHub helpers ----------
def gh(path: str, method="GET", **kwargs):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN missing.")
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/vnd.github+json"
    url = f"https://api.github.com{path}"
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if r.status_code >= 400:
        raise SystemExit(f"GitHub API {method} {path} failed: {r.status_code} {r.text[:400]}")
    return r.json()

def pr_context() -> Tuple[str, str, int, str]:
    repo_full = os.getenv("GITHUB_REPOSITORY", "")
    owner, repo = repo_full.split("/", 1)
    event_path = os.getenv("GITHUB_EVENT_PATH")
    with open(event_path, "r", encoding="utf-8") as f:
        evt = json.load(f)
    pr = evt["pull_request"]
    number = pr["number"]
    head_sha = pr["head"]["sha"]
    return owner, repo, number, head_sha

def list_changed_files(owner: str, repo: str, pr_number: int) -> List[Dict]:
    files = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    out = []
    for f in files:
        if not f["filename"].endswith(".py"):
            continue
        item = {
            "path": f["filename"],
            "basename": f["filename"].split("/")[-1],
            "status": f.get("status"),
            "patch": f.get("patch"),
            "raw_url": f.get("raw_url"),
        }
        if item["raw_url"]:
            raw = requests.get(item["raw_url"], timeout=30)
            if raw.ok:
                item["content"] = raw.text
        out.append(item)
    return out

# ---------- Diff helpers ----------
def candidate_anchor_lines(patch: str) -> List[int]:
    """
    Return a list of RIGHT-side (HEAD) line numbers from the diff:
    - all '+' lines first (best anchors),
    - then a few context lines ' ' as backup.
    Always returns at least [1].
    """
    if not patch:
        return [1]

    anchors_add: List[int] = []
    anchors_ctx: List[int] = []
    head_line = 1

    for ln in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m:
            head_line = int(m.group(1))
            continue

        if ln.startswith('+') and not ln.startswith('+++'):
            anchors_add.append(head_line)
            head_line += 1
            continue

        if ln.startswith(' ') or (ln and ln[0] not in '+-@'):
            # context exists on RIGHT
            anchors_ctx.append(head_line)
            head_line += 1
            continue

        if ln.startswith('-') and not ln.startswith('---'):
            # deletion: LEFT only, do not advance RIGHT
            continue

    anchors = anchors_add + anchors_ctx
    return anchors or [1]

# ---------- Posting review ----------
def post_summary_review(owner: str, repo: str, pr_number: int, head_sha: str, body_md: str):
    gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
       method="POST",
       json={"commit_id": head_sha, "event": "COMMENT", "body": body_md[:65000]})

def post_inline_comment(owner: str, repo: str, pr_number: int, head_sha: str,
                        path: str, line: int, body_md: str) -> bool:
    payload = {
        "body": body_md[:65000],
        "commit_id": head_sha,
        "path": path,
        "side": "RIGHT",
        "line": int(line),
    }
    try:
        res = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/comments", method="POST", json=payload)
        print(f"[bot] inline OK: {path} @ {line} id={res.get('id')}")
        return True
    except SystemExit as e:
        print(f"[bot] inline FAIL: {path} @ {line} -> {e}")
        return False

# ---------- LLM output parsing ----------
# Accept headings like: "## path/to/file.py" for files
FILE_H_RE = re.compile(r"^##\s+(.+\.py)\s*$")
# Inside a file section, split parts by: "### <anything>"
PART_H_RE = re.compile(r"^###\s+(.+)\s*$")

def parse_per_file_parts(review_md: str, files: List[Dict]) -> Dict[str, List[str]]:
    """
    Returns { file_path -> [part_markdown, ...] }
    - File sections start with '## path/to/file.py'
    - Parts inside a file start with '### ...'
    - If no parts are found in a file section, the whole file section is one part.
    - If the LLM omitted a file entirely, create a minimal placeholder.
    """
    # 1) Split into file sections
    sections: Dict[str, List[str]] = {}
    cur_file = None
    buf: List[str] = []

    lines = review_md.splitlines()
    for line in lines:
        m_file = FILE_H_RE.match(line.strip())
        if m_file:
            # flush previous
            if cur_file and buf:
                sections[cur_file] = buf[:]
            cur_file = m_file.group(1).strip()
            buf = [line]
        else:
            if cur_file:
                buf.append(line)
    if cur_file and buf:
        sections[cur_file] = buf[:]

    # 2) For each section, split into parts by '###'
    per_file_parts: Dict[str, List[str]] = {}
    lower_keys = {k.lower(): k for k in sections.keys()}

    for f in files:
        path = f["path"]
        key = sections.get(path)
        if key is None:
            # try case-insensitive
            k2 = lower_keys.get(path.lower())
            if k2:
                key = sections.get(k2)

        if key is None:
            # placeholder: ensure every changed file gets at least one comment
            per_file_parts[path] = [f"## {path}\n_No explicit suggestions generated for this file._"]
            continue

        # Split to parts
        parts: List[List[str]] = []
        cur_part: List[str] = []
        for ln in key:
            if PART_H_RE.match(ln.strip()):
                if cur_part:
                    parts.append(cur_part)
                cur_part = [ln]
            else:
                if not cur_part:
                    cur_part = []
                cur_part.append(ln)
        if cur_part:
            parts.append(cur_part)

        if not parts:
            parts = [key]  # whole file as one part

        per_file_parts[path] = ["\n".join(p).strip() for p in parts]

    return per_file_parts

# ---------- Main ----------
def main():
    owner, repo, pr_number, head_sha = pr_context()

    # 1) Confluence spec
    spec_text = fetch_confluence_spec()

    # 2) Changed Python files
    files = list_changed_files(owner, repo, pr_number)
    if not files:
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST",
           json={"body": "🤖 Review Bot: No Python file changes detected."})
        return

    print(f"[bot] changed .py files: {[f['path'] for f in files]}")

    # 3) LLM review (markdown over ALL files)
    review_md = review(spec_text, files)

    # 4) Build per-file, per-part suggestions (guarantee every file has ≥1 part)
    per_file_parts = parse_per_file_parts(review_md, files)

    summary_md = (
        "### 🤖 Review Bot\n"
        f"**Spec:** {os.getenv('CONF_URL')}\n\n"
        "Inline comments are posted **per part** for **every changed Python file**. "
        "If anchoring a part fails, a fallback summary comment is added."
    )

    # 5) Post summary review
    post_summary_review(owner, repo, pr_number, head_sha, summary_md)

    # 6) Post inline comments per part, with robust anchoring
    meta = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    patch_by_path = {m["filename"]: (m.get("patch") or "") for m in meta}

    posted = 0
    failures: Dict[str, List[str]] = {}

    for f in files:
        path = f["path"]
        parts = per_file_parts.get(path, [])
        anchors = candidate_anchor_lines(patch_by_path.get(path, ""))
        if not anchors:
            anchors = [1]

        # spread parts across anchor lines; reuse last anchor if not enough
        for idx, part_md in enumerate(parts):
            line = anchors[idx] if idx < len(anchors) else anchors[-1]
            ok = post_inline_comment(owner, repo, pr_number, head_sha, path, line, part_md)
            if ok:
                posted += 1
            else:
                failures.setdefault(path, []).append(part_md)

    # 7) Fallback: if some parts failed, or nothing posted at all
    if failures or posted == 0:
        body = ["### 🤖 Review Bot (fallback)"]
        if posted == 0:
            body.append("No valid inline anchors were available across files.")
        else:
            body.append("Inline anchoring failed for these parts:")
            for p, lst in failures.items():
                body.append(f"- `{p}`: {len(lst)} part(s)")
        body.append("\n#### Suggestions\n")
        for p, lst in (failures.items() if failures else per_file_parts.items()):
            if isinstance(lst, list) and lst and isinstance(lst[0], str):
                # failures[p] is a list of part markdown
                parts = lst
            else:
                # per_file_parts[path] -> list[str]
                parts = per_file_parts[p]
            body.append(f"## {p}")
            body.append("\n\n---\n\n".join(parts))
        gh(f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
           method="POST", json={"body": "\n\n".join(body)[:65000]})
        print(f"[bot] fallback posted ({posted} inline OK).")
    else:
        print(f"[bot] inline comments posted: {posted}")

if __name__ == "__main__":
    main()
