# main.py
import os, re, html, json, base64, requests, yaml
from typing import List, Dict, Tuple
from llm import review
from rich import print

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
        # very quick HTML -> text
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

# Parse unified diff to find a safe line (in HEAD) to attach a review comment.
def first_added_line_from_patch(patch: str) -> int:
    """
    Returns the first added line number in the HEAD file from a unified diff hunk.
    If none found, returns 1.
    """
    if not patch:
        return 1
    line_num_head = 1
    for line in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            line_num_head = int(m.group(1))
            continue
        if line.startswith('+') and not line.startswith('+++'):
            return line_num_head
        if not line.startswith('-') and not line.startswith('@@') and not line.startswith('+++') and not line.startswith('---'):
            line_num_head += 1
        if line.startswith('+') and not line.startswith('+++'):
            line_num_head += 1
    return max(1, line_num_head)

# ---------- Posting review ----------
def create_review_with_comments(owner: str, repo: str, pr_number: int, head_sha: str, summary_md: str, file_suggestions: Dict[str, str]):
    """
    file_suggestions: { path -> markdown body (should include ```suggestion blocks```) }
    We attach one inline comment per file at a safe-added line (or line 1).
    """
    files = gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
    comments = []
    for f in files:
        p = f["filename"]
        if p in file_suggestions and p.endswith(".py"):
            patch = f.get("patch") or ""
            line = first_added_line_from_patch(patch)
            comments.append({
                "path": p,
                "side": "RIGHT",
                "line": line,
                "body": file_suggestions[p][:65000],  # API limit safety
            })

    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": summary_md[:65000],
        "comments": comments[:50],  # safety
    }
    gh(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", method="POST", json=payload)

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

    # 3) LLM review (markdown)
    review_md = review(spec_text, files)

    # 4) Split summary + per-file suggestion bodies
    # Simple heuristic: extract per-file sections starting with "## path"
    file_suggestions = {}
    cur_path = None
    cur_buf: List[str] = []
    for line in review_md.splitlines():
        m = re.match(r"^##\s+(.+\.py)\b", line.strip())
        if m:
            if cur_path and cur_buf:
                file_suggestions[cur_path] = "\n".join(cur_buf).strip()
            cur_path = m.group(1)
            cur_buf = [line]
        else:
            if cur_path:
                cur_buf.append(line)
    if cur_path and cur_buf:
        file_suggestions[cur_path] = "\n".join(cur_buf).strip()

    summary_md = "### 🤖 Review Bot\n" \
                 f"**Spec:** {os.getenv('CONF_URL')}\n\n" \
                 "Below are inline review comments with suggestions. " \
                 "You can click **Apply suggestion** if it looks good."

    # 5) Post a PR review with inline comments (acceptance supported)
    create_review_with_comments(owner, repo, pr_number, head_sha, summary_md, file_suggestions)

    print(f"Posted review with {len(file_suggestions)} inline comment(s).")

if __name__ == "__main__":
    main()
