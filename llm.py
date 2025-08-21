# llm.py
import os
from typing import List, Dict
from langchain_openai import AzureChatOpenAI

SYSTEM = (
    "You are a senior Python code reviewer.\n"
    "Use the provided SPEC (authoritative) and the DIFFS to review changes.\n"
    "Be concise. Propose actionable fixes with code blocks.\n"
    "When possible, output GitHub suggestion blocks using the exact syntax:\n"
    "```suggestion\n<replacement code>\n```\n"
    "Only comment on issues relevant to the SPEC or clear best practices (security, correctness, style).\n"
    "If no issues, say so for that file.\n"
)

def make_llm() -> AzureChatOpenAI:
    """
    Reads standard Azure OpenAI env vars (set in the workflow):
    - AZURE_OPENAI_API_KEY
    - AZURE_OPENAI_ENDPOINT
    - AZURE_OPENAI_API_VERSION
    - AZURE_OPENAI_DEPLOYMENT
    """
    # LangChain picks these up from env automatically; no hardcoding here.
    return AzureChatOpenAI(
        azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        temperature=0,
        max_tokens=800,   # room for suggestions
        streaming=False,
    )

def build_prompt(spec_text: str, files: List[Dict]) -> str:
    parts = []
    parts.append("# SPEC (from Confluence)\n")
    parts.append(spec_text.strip()[:20000])
    parts.append("\n\n# CHANGED FILES (unified diffs + snapshots)\n")
    for f in files:
        parts.append(f"## {f['path']} ({f.get('status','')})\n")
        if f.get("patch"):
            parts.append("```diff\n" + f["patch"][:20000] + "\n```")
        if f.get("content"):
            parts.append(f"\n<current file snapshot: {f['path']}>\n```python\n{f['content'][:20000]}\n```\n")
    parts.append(
        "\n# Review Task\n"
        "- For each file, list issues as bullets.\n"
        "- Cite SPEC when applicable.\n"
        "- Provide **GitHub suggestion blocks** for exact replacements.\n"
        "- Keep suggestions minimal and safe.\n"
        "- Output markdown.\n"
    )
    return "\n".join(parts)

def review(spec_text: str, files: List[Dict]) -> str:
    llm = make_llm()
    prompt = build_prompt(spec_text, files)
    resp = llm.invoke([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt}
    ])
    return getattr(resp, "content", str(resp))
