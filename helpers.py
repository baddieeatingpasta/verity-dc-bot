"""Pure helper functions for Verity (no Discord code here, so they're easy to test)."""

import io
import json
import re
from pathlib import PurePosixPath

# ---------------------------------------------------------------- cleaning
# Citation markers like [1], [2][3], [1, 2], [1-3], [?]
CITATION_RE = re.compile(
    r"[ \t]*(?:\[\s*(?:\d+|\?)(?:\s*[,;\u2013-]\s*(?:\d+|\?))*\s*\])+"
)
# Code we must never touch: fenced blocks and `inline code`
PROTECT_RE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)


def _tidy_text(text: str) -> str:
    text = CITATION_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    return text


def clean_answer(text: str) -> str:
    """Strip citation markers from prose only, leaving code untouched."""
    parts = PROTECT_RE.split(text)
    out = [p if i % 2 else _tidy_text(p) for i, p in enumerate(parts)]
    return "".join(out).strip()


# ---------------------------------------------------------------- code detection
def guess_lang(code: str) -> str:
    c = code.strip()
    if not c:
        return ""
    first = c.splitlines()[0].strip()
    if c[0] in "{[":
        try:
            json.loads(c)
            return "json"
        except ValueError:
            pass
    if re.match(r"(\$ |#!/|sudo |apt(-get)? |pip3? |npm |npx |yarn |git |cd |ls\b|chmod |chown |curl |wget |"
                r"docker |echo |cat |mkdir |rm |mv |cp |systemctl |export |brew |tar |ssh |grep )", first):
        return "bash"
    if re.search(r"\bselect\b.+\bfrom\b", c, re.I | re.S) or re.search(
        r"\b(insert\s+into|create\s+table|alter\s+table|drop\s+table|delete\s+from)\b", c, re.I
    ):
        return "sql"
    if c.startswith("<") and re.search(r"</?\w+", c):
        return "html"
    if re.search(r"#include\s*<", c):
        return "cpp"
    if re.search(r"\b(public\s+(static\s+)?(class|void)|System\.out\.)", c):
        return "java"
    if re.search(r"^\s*(def |class \w+.*:|import \w+|from \S+ import|async def |elif |print\()", c, re.M) or "self." in c:
        return "python"
    if re.search(r"\b(const|let|var)\s+\w+\s*=|function\s*\w*\(|=>|console\.log|require\(", c):
        return "javascript"
    return ""


CODE_LINE_RE = re.compile(
    r"^\s*(def |class |import |from \S+ import|return\b|elif |else:|try:|except|with .+ as |async def |await |"
    r"function |const |let |var |SELECT |INSERT |UPDATE |DELETE |CREATE |ALTER |DROP |#include|public |private |"
    r"print\(|console\.log|\}|\{|@\w+)|.*[;{}]\s*$|.*:\s*$|^ {4,}\S",
)
CMD_LINE_RE = re.compile(
    r"^\s*(\$ |sudo |apt(-get)? |pip3? install|npm |npx |git |cd |ls\b|chmod |curl |wget |docker |systemctl |python3? )"
)


def autoformat_code(text: str) -> str:
    """Make sure code is inside fenced blocks with a language tag."""
    # close an unterminated fence
    if text.count("```") % 2 == 1:
        text += "\n```"

    if "```" in text:
        def add_lang(m):
            lang, code = m.group(1).strip(), m.group(2)
            return f"```{lang or guess_lang(code)}\n{code}```"
        return FENCE_RE.sub(add_lang, text)

    lines = text.splitlines()
    non_empty = [l for l in lines if l.strip()]

    # whole answer is code
    if len(non_empty) >= 3 and sum(bool(CODE_LINE_RE.match(l)) for l in non_empty) / len(non_empty) >= 0.6:
        return f"```{guess_lang(text)}\n{text.strip()}\n```"

    # wrap standalone command lines
    out, buf = [], []

    def flush():
        if buf:
            out.append("```bash\n" + "\n".join(buf) + "\n```")
            buf.clear()

    for l in lines:
        if CMD_LINE_RE.match(l):
            buf.append(l.strip().removeprefix("$ ").strip())
        else:
            flush()
            out.append(l)
    flush()
    return "\n".join(out)


def extract_code_blocks(text: str):
    return [(m.group(1).strip(), m.group(2).strip("\n")) for m in FENCE_RE.finditer(text)]


EXT_FOR_LANG = {
    "python": "py", "py": "py", "bash": "sh", "sh": "sh", "shell": "sh", "sql": "sql",
    "javascript": "js", "js": "js", "typescript": "ts", "json": "json", "html": "html",
    "java": "java", "cpp": "cpp", "c": "c", "css": "css", "yaml": "yml",
}


def ext_for(lang: str) -> str:
    return EXT_FOR_LANG.get(lang.lower(), "txt")


# ---------------------------------------------------------------- pagination
def _split_code(lang: str, code: str, limit: int):
    head = f"```{lang}\n"
    budget = limit - len(head) - 4
    if len(code) <= budget:
        return [f"{head}{code}\n```"]
    chunks, cur = [], ""
    for line in code.split("\n"):
        while len(line) > budget:  # absurdly long single line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:budget])
            line = line[budget:]
        if cur and len(cur) + 1 + len(line) > budget:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)
    return [f"{head}{c}\n```" for c in chunks]


def _split_text(para: str, limit: int):
    if len(para) <= limit:
        return [para]
    pieces = re.split(r"(?<=[.!?])\s+|\n", para)
    chunks, cur = [], ""
    for p in pieces:
        while len(p) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:limit])
            p = p[limit:]
        if cur and len(cur) + 1 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = p if not cur else cur + " " + p
    if cur:
        chunks.append(cur)
    return chunks


def paginate(text: str, limit: int = 3800):
    """Split markdown into pages that never cut a code block in half without re-fencing it."""
    chunks = []
    for part in re.split(r"(```[^\n`]*\n.*?```)", text, flags=re.S):
        if not part.strip():
            continue
        m = FENCE_RE.match(part)
        if m:
            chunks.extend(_split_code(m.group(1).strip(), m.group(2).strip("\n"), limit))
        else:
            for para in re.split(r"\n{2,}", part):
                if para.strip():
                    chunks.extend(_split_text(para.strip(), limit))
    pages, cur = [], ""
    for c in chunks:
        if cur and len(cur) + 2 + len(c) > limit:
            pages.append(cur)
            cur = c
        else:
            cur = c if not cur else cur + "\n\n" + c
    if cur:
        pages.append(cur)
    return pages or ["(empty answer)"]


# ---------------------------------------------------------------- links & files
URL_RE = re.compile(r"https?://[^\s<>]+")
OTHER_SCHEME_RE = re.compile(r"\b(?:ftp|file|ssh|sftp|magnet):[^\s<>]+", re.I)
UNSUPPORTED_DOMAINS = {
    "youtube.com": "video", "youtu.be": "video", "tiktok.com": "video", "twitch.tv": "video",
    "twitter.com": "login-only social", "x.com": "login-only social", "instagram.com": "login-only social",
    "facebook.com": "login-only social", "discord.com": "Discord link", "discord.gg": "Discord invite",
}


def extract_urls(text: str):
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(").,;:!?>\"'")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def strip_urls(text: str) -> str:
    text = URL_RE.sub("", text or "")
    text = OTHER_SCHEME_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def find_unsupported_links(text: str):
    return OTHER_SCHEME_RE.findall(text or "")


def check_url(url: str):
    """Return an explanation if we know the link can't be read, else None."""
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split(":")[0].lower()
    host = host.removeprefix("www.")
    for dom, why in UNSUPPORTED_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return f"{why} links aren't supported"
    return None


TEXT_EXTS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".xml", ".html", ".htm", ".css", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".bash", ".bat", ".ps1", ".lua", ".kt", ".swift", ".r",
}
SUPPORTED_SUMMARY = "PDF, plain text and code files (.txt, .md, .py, .js, .json, .csv, .sql, .sh, …)"
MAX_PDF_PAGES = 60


def bytes_to_text(filename: str, data: bytes):
    """Return (text, None) on success or (None, reason) when the file can't be read."""
    ext = PurePosixPath(filename.lower()).suffix

    if ext == ".pdf" or data[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None, "PDF support isn't installed on the bot (`pip install pypdf`)"
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                return None, "the PDF is password-protected"
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES]).strip()
        except Exception as e:
            return None, f"couldn't parse the PDF ({type(e).__name__})"
        if not text:
            return None, "the PDF has no selectable text (it looks scanned, and I can't do OCR)"
        return text, None

    if ext not in TEXT_EXTS and b"\x00" in data[:4096]:
        return None, f"unsupported file type — I can read {SUPPORTED_SUMMARY}"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    text = text.strip()
    if not text:
        return None, "the file is empty"
    return text, None
