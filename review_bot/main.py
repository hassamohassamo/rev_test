import os, re, html, json, requests
from typing import List, Dict, Tuple
from .llm import review  # if not a package, change to: from llm import review

# ---- helpers ----
def gh(p: str, m="GET", **kw):
    t = os.getenv("GITHUB_TOKEN"); 
    if not t: raise SystemExit("GITHUB_TOKEN missing.")
    h = kw.pop("headers", {})
    h.update({"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json"})
    r = requests.request(m, f"https://api.github.com{p}", headers=h, timeout=30, **kw)
    if r.status_code >= 400: raise SystemExit(f"GitHub {m} {p}: {r.status_code} {r.text[:400]}")
    return r.json()

def ctx() -> Tuple[str,str,int,str]:
    o,r = os.getenv("GITHUB_REPOSITORY","").split("/",1)
    with open(os.getenv("GITHUB_EVENT_PATH"),"r",encoding="utf-8") as f: e=json.load(f)
    pr=e["pull_request"]; return o,r,pr["number"],pr["head"]["sha"]

def spec() -> str:
    u,usr,tok = os.getenv("CONF_URL"), os.getenv("CONF_USERNAME"), os.getenv("CONF_API")
    if not (u and usr and tok): raise SystemExit("Missing CONF_*")
    j = requests.get(u,auth=(usr,tok),timeout=30).json()
    s = (((j or {}).get("body") or {}).get("storage") or {}).get("value") or ""
    if s:
        x=re.sub(r"</p>","\n",s,flags=re.I); x=re.sub(r"<br\s*/?>","\n",x,flags=re.I); x=re.sub(r"<[^>]+>","",x)
        return html.unescape(x).strip()
    return json.dumps(j,indent=2)

def changed(o:str,r:str,n:int)->List[Dict]:
    fs=gh(f"/repos/{o}/{r}/pulls/{n}/files"); out=[]
    for f in fs:
        if not f["filename"].endswith(".py"): continue
        it={"path":f["filename"],"patch":f.get("patch") or "","raw":f.get("raw_url")}
        if it["raw"]:
            q=requests.get(it["raw"],timeout=30); 
            if q.ok: it["content"]=q.text
        out.append(it)
    return out

def anchors(p:str)->List[int]:
    if not p: return [1]
    a,c,head=[],[],1
    for ln in p.splitlines():
        m=re.match(r"@@ -\d+(?:,\d+)? \+(\d+)",ln)
        if m: head=int(m.group(1)); continue
        if ln.startswith('+') and not ln.startswith('+++'): a.append(head); head+=1; continue
        if ln.startswith(' ') or (ln and ln[0] not in '+-@'): c.append(head); head+=1; continue
        if ln.startswith('-') and not ln.startswith('---'): continue
    return (a+c) or [1]

def post_sum(o,r,n,sha,body):
    gh(f"/repos/{o}/{r}/pulls/{n}/reviews",method="POST",
       json={"commit_id":sha,"event":"COMMENT","body":body[:65000]})

def post_inline(o,r,n,sha,path,line,body)->bool:
    try:
        gh(f"/repos/{o}/{r}/pulls/{n}/comments",method="POST",
           json={"body":body[:65000],"commit_id":sha,"path":path,"side":"RIGHT","line":int(line)})
        return True
    except SystemExit:
        return False

# ---- LLM parsing ----
FRE=re.compile(r"^##\s+(.+\.py)\s*$"); PRE=re.compile(r"^###\s+(.+)\s*$"); SRE=re.compile(r"```suggestion\b",re.I)
def split_parts(md:str, files:List[Dict])->Dict[str,List[str]]:
    secs={}; cur=None; buf=[]
    for ln in md.splitlines():
        m=FRE.match(ln.strip())
        if m: 
            if cur and buf: secs[cur]=buf[:]
            cur=m.group(1).strip(); buf=[ln]
        elif cur: buf.append(ln)
    if cur and buf: secs[cur]=buf[:]
    out={}
    for f in files:
        p=f["path"]; key=secs.get(p) or next((secs[k] for k in secs if k.lower()==p.lower()),None)
        if not key: out[p]=[f"## {p}\n_No explicit suggestions._"]; continue
        parts=[]; cur=[]
        for ln in key:
            if PRE.match(ln.strip()):
                if cur: parts.append(cur); cur=[ln]
            else:
                (cur if cur else []).append(ln)
        if cur: parts.append(cur)
        out[p]=["\n".join(x).strip() for x in (parts or [key])]
    return out

# ---- static rules ----
def static_parts(p:str, txt:str)->List[str]:
    out=[]
    if "BLACK_DUCK" in txt or re.search(r"\beval\s*\(",txt):
        out.append(f"## {p}\n### Replace unsafe eval\n```suggestion\nimport ast\n\ndef parse_settings_eval(s:str)->dict:\n    try:\n        v=ast.literal_eval(s)\n        return v if isinstance(v,dict) else {{}}\n    except (ValueError,SyntaxError):\n        return {{}}\n```\n")
    if re.search(r"\byaml\.load\s*\(",txt) and "SafeLoader" not in txt:
        out.append(f"## {p}\n### Use yaml.safe_load\n```suggestion\nimport yaml\n\ndef load_yaml(t:str)->dict:\n    return yaml.safe_load(t) or {{}}\n```\n")
    if re.search(r"\bdef\s+\w+\(.*=\[\]|\{\}",txt):
        out.append(f"## {p}\n### Avoid mutable defaults\n```suggestion\ndef join_words(words:list[str]|None=None,sep:str=' ')->str:\n    words=[] if words is None else words\n    return sep.join(words)\n```\n")
    if re.search(r"except\s+Exception\s*:",txt):
        out.append(f"## {p}\n### Avoid broad except\n```suggestion\ndef load_config(path:str)->dict:\n    try:\n        with open(path,'r',encoding='utf-8') as f:\n            data=f.read()\n    except FileNotFoundError:\n        return {{}}\n    return {{}}\n```\n")
    if re.search(r"\bf\s*=\s*open\(",txt):
        out.append(f"## {p}\n### Use context manager\n```suggestion\ndef load_config(path:str)->dict:\n    with open(path,'r',encoding='utf-8') as f:\n        data=f.read()\n    return {{}}\n```\n")
    return out

def ensure_actionable(files:List[Dict], parts:Dict[str,List[str]], patches:Dict[str,str])->Dict[str,List[Tuple[str,int]]]:
    out={}
    for f in files:
        p=f["path"]; txt=f.get("content","") or ""; lines=txt.splitlines() or [""]
        ps=(parts.get(p,[])+static_parts(p,txt)) or [f"## {p}\n_No issues detected._"]
        an=anchors(patches.get(p,"")) or [1]; cooked=[]
        for i,md in enumerate(ps):
            ln=an[i] if i<len(an) else an[-1]
            if not SRE.search(md or ""):
                src = lines[ln-1] if 1<=ln<=len(lines) else ""
                nl = (src if src.strip() else "#") + ("  # review-bot" if "review-bot" not in src else "")
                md = md + "\n\n```suggestion\n" + nl + "\n```"
            cooked.append((md,ln))
        out[p]=cooked
    return out

# ---- main ----
def main():
    o,r,n,sha=ctx()
    sp=spec()
    fs=changed(o,r,n)
    if not fs:
        gh(f"/repos/{o}/{r}/issues/{n}/comments",method="POST",json={"body":"🤖 Review Bot: No Python changes."}); return
    md=review(sp,fs)
    per=split_parts(md,fs)
    meta=gh(f"/repos/{o}/{r}/pulls/{n}/files")
    lines=ensure_actionable(fs,per,{m["filename"]:(m.get("patch") or "") for m in meta})
    post_sum(o,r,n,sha,f"### 🤖 Review Bot\n**Spec:** {os.getenv('CONF_URL')}\n\nSuggestions are provided for every section.")
    posted=0
    for f in fs:
        for body,ln in lines.get(f["path"],[]):
            if post_inline(o,r,n,sha,f["path"],ln,body): posted+=1
    if posted==0:
        allb=["### 🤖 Review Bot (fallback)"]
        for p,arr in lines.items(): allb.append(f"## {p}\n" + "\n\n---\n\n".join(md for md,_ in arr))
        gh(f"/repos/{o}/{r}/issues/{n}/comments",method="POST",json={"body":"\n\n".join(allb)[:65000]})

if __name__=="__main__": main()
