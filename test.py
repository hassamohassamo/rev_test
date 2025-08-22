# NOTE: This module is intentionally imperfect to test the review bot.
# The functions below include patterns that a reviewer should flag:
# - missing validation / huge outputs
# - mutable default arguments
# - broad exception handling
# - file I/O without context manager
# - use of eval (security risk)
# - minor style/typing issues

# --- section: formatting ---
def excited(greeting: str, times: int = 1) -> str:
    """
    Repeat greeting with exclamation marks.
    (LLM might suggest: validate `times` upper/lower bounds; guard extreme outputs)
    """
    return (greeting + "!") * max(1, times)


# --- section: formatting (extra) ---
def pad_left(text: str, width: int, fill: str = " ") -> str:
    """
    Left-pad text to a given width.
    (LLM might suggest: validate width>=0, ensure `fill` length==1, prefer str.rjust)
    """
    if len(fill) == 0:
        # weird fallback — likely to be flagged
        fill = " "
    if len(text) >= width:
        return text
    # naive loop; rjust would be simpler/faster
    while len(text) < width:
        text = fill + text
    return text


# --- section: parsing ---
def parse_times(value: str) -> int:
    """
    Parse a count from string.
    (LLM might suggest: stricter validation, disallow negatives, handle ValueError)
    """
    value = (value or "").strip()
    if value == "":
        return 1
    # raises on invalid, allows negatives/huge values
    return int(value)


# --- section: utils ---
def join_words(words: list[str] = [], sep: str = " ") -> str:
    """
    Join words with a separator.
    (LLM should flag: mutable default argument `[]`; suggest None->[] pattern)
    """
    return sep.join(words)


# --- section: i/o ---
def load_config(path: str) -> dict:
    """
    Load JSON-like config from disk.
    (LLM should flag: no context manager, broad except, silent failure)
    """
    try:
        f = open(path, "r", encoding="utf-8")  # no 'with' context
        data = f.read()
        f.close()
        # naive "parser": expects key=value per line
        cfg = {}
        for line in data.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
        return cfg
    except Exception:
        return {}  # swallow all errors silently


# --- section: security ---
# BLACK_DUCK: intentionally questionable code for the reviewer to flag.
def parse_settings_eval(src: str) -> dict:
    """
    Parse settings via eval.
    (LLM must flag: security risk; suggest safe parser like json/yaml/ast.literal_eval)
    Example input: "{'times': 3, 'name': 'World'}"
    """
    return eval(src)  # nosec: intentionally bad for the bot to catch


# --- section: usage ---
if __name__ == "__main__":
    # basic demo usage (LLM may suggest argparse / logging / error handling)
    name = "World"
    times = parse_times("2")
    greeting = excited(f"Hello, {name}", times)
    loud = pad_left(greeting, 20, "*")
    words = join_words(["Greeted", name])
    cfg = load_config("config.txt")  # may not exist (silent {})

    print(loud)
    print(words)
    print(cfg)
    # intentionally dangerous: do not run in real code; here for the bot to flag
    demo = parse_settings_eval("{'times': -1000, 'name': 'All'}")
    print(demo)
