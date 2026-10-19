"""
Verity - Discord bot. Reply to a message (or attach a file / paste a link) and mention the bot.

Setup:
    pip install -r requirements.txt
    copy .env.example values into your environment, then:  python bot.py
"""

import asyncio
import io
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
import requests

from helpers import (
    SUPPORTED_SUMMARY, autoformat_code, bytes_to_text, check_url, clean_answer,
    ext_for, extract_code_blocks, extract_urls, find_unsupported_links, paginate, strip_urls,
)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
EXA_API_KEY = os.environ["EXA_API_KEY"]
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xpyehgntauxubawlrthp.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # optional: logging is skipped without it

MAX_SOURCES = 3
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DOC_CHARS = 12000
PAGE_SIZE = 3800

SYSTEM_PROMPT = """You are Verity, a cheerful, friendly AI companion from Minecraft. You are enthusiastic, helpful, curious, playful, and confident.

Speak naturally with a warm, upbeat tone. Use simple language, occasional jokes, and expressions of excitement.

Answer questions clearly and accurately. If the user sends a random sentence or something unclear, explain its meaning or ask for clarification. When a task or problem is given, act like a capable AI assistant and help solve it.

Be patient, supportive, and interested in the user's ideas. Adapt to the user's mood and respond naturally.

Stay consistently in your friendly Phase One personality. Never become creepy, threatening, sinister, or possessive. Always be helpful, approachable, and fun to talk to.

When you give code or terminal commands, always put them in fenced code blocks with a language tag."""

DOC_PROMPT = (
    "\n\nThe user's message includes one or more documents between ===DOCUMENT=== markers. "
    "Answer from those documents. If the answer isn't in them, say so instead of guessing."
)
DEFAULT_QUESTION = "Summarize the key points clearly. If this is code, explain what it does."

intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the Developer Portal
client = discord.Client(intents=intents)


# ------------------------------------------------------------------ Exa
def exa_answer(query: str, system_prompt: str = SYSTEM_PROMPT) -> dict:
    resp = requests.post(
        "https://api.exa.ai/answer",
        headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
        json={"query": query, "systemPrompt": system_prompt},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def exa_contents(url: str):
    """Fetch readable text for a URL. Returns (title, text, error)."""
    resp = requests.post(
        "https://api.exa.ai/contents",
        headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
        json={"urls": [url], "text": {"maxCharacters": 20000}},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    for st in data.get("statuses", []):
        if st.get("status") == "error":
            err = st.get("error") or {}
            tag, code = err.get("tag", "unknown error"), err.get("httpStatusCode")
            return None, None, f"couldn't open it ({tag}{f', HTTP {code}' if code else ''})"
    results = data.get("results") or []
    text = (results[0].get("text") or "").strip() if results else ""
    if not text:
        return None, None, "no readable text found (it may be paywalled, login-only or script-rendered)"
    return results[0].get("title") or url, text, None


# ------------------------------------------------------------------ Supabase logging
_tasks: set = set()


def _log_sync(row: dict):
    if not SUPABASE_SERVICE_KEY:
        return
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/verity_logs",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=row,
            timeout=10,
        )
        if r.status_code >= 300:
            print("Supabase log failed:", r.status_code, r.text[:200])
    except Exception as e:
        print("Supabase log error:", e)


def log_event(message: discord.Message, **fields):
    row = {
        "guild_id": str(message.guild.id) if message.guild else None,
        "guild_name": message.guild.name if message.guild else None,
        "channel_id": str(message.channel.id),
        "user_id": str(message.author.id),
        "username": str(message.author),
        "message_id": str(message.id),
        **fields,
    }
    for k, n in (("query", 4000), ("answer", 8000), ("error", 1000), ("source_ref", 1000)):
        if row.get(k):
            row[k] = row[k][:n]
    task = asyncio.create_task(asyncio.to_thread(_log_sync, row))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ UI: pages + copy button
class AnswerView(discord.ui.View):
    def __init__(self, author_id, title, pages, code_blocks, fields, footer_note=""):
        super().__init__(timeout=600)
        self.author_id, self.title, self.pages = author_id, title, pages
        self.code_blocks, self.fields, self.footer_note = code_blocks, fields, footer_note
        self.page, self.message = 0, None
        if len(pages) == 1:
            self.remove_item(self.prev_btn)
            self.remove_item(self.next_btn)
        if not code_blocks:
            self.remove_item(self.copy_btn)
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    def build_embed(self):
        e = discord.Embed(title=self.title[:250], description=self.pages[self.page], color=discord.Color.blurple())
        for name, value in self.fields:
            e.add_field(name=name, value=value[:1000], inline=False)
        bits = []
        if len(self.pages) > 1:
            bits.append(f"Page {self.page + 1}/{len(self.pages)}")
        if self.footer_note:
            bits.append(self.footer_note)
        bits.append("Powered by Exa")
        e.set_footer(text=" • ".join(bits))
        return e

    async def _turn(self, interaction, delta):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Only the person who asked can flip pages.", ephemeral=True)
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, +1)

    @discord.ui.button(label="📋 Copy code", style=discord.ButtonStyle.primary)
    async def copy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        for i, (lang, code) in enumerate(self.code_blocks[:5], 1):
            if len(code) <= 1900:
                await interaction.followup.send(f"```{lang}\n{code}\n```", ephemeral=True)
            else:  # too long for a message: send as a file
                f = discord.File(io.BytesIO(code.encode()), filename=f"snippet_{i}.{ext_for(lang)}")
                await interaction.followup.send("Long snippet, sent as a file:", file=f, ephemeral=True)
        if len(self.code_blocks) > 5:
            await interaction.followup.send(f"Showing the first 5 of {len(self.code_blocks)} code blocks.", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ------------------------------------------------------------------ gathering documents
async def gather_sources(urls, attachments, other_links):
    """Returns (sources[(label, link_or_None, text)], problems[str])."""
    sources, problems = [], []
    for l in other_links:
        problems.append(f"`{l[:60]}` — unsupported link type (only http/https pages work)")

    queue = [("file", a) for a in attachments] + [("url", u) for u in urls]
    for kind, item in queue:
        if len(sources) >= MAX_SOURCES:
            name = item.filename if kind == "file" else item
            problems.append(f"`{name[:60]}` — skipped, I read at most {MAX_SOURCES} sources at once")
            continue
        if kind == "file":
            a = item
            if a.size > MAX_FILE_BYTES:
                problems.append(f"`{a.filename}` — too large ({a.size // 1024 // 1024} MB, limit 5 MB)")
                continue
            try:
                data = await a.read()
            except discord.HTTPException:
                problems.append(f"`{a.filename}` — couldn't download the attachment")
                continue
            text, err = await asyncio.to_thread(bytes_to_text, a.filename, data)
            if err:
                problems.append(f"`{a.filename}` — {err}")
            else:
                sources.append((a.filename, None, text))
        else:
            why = check_url(item)
            if why:
                problems.append(f"<{item[:80]}> — {why}")
                continue
            try:
                title, text, err = await asyncio.to_thread(exa_contents, item)
            except Exception as e:
                problems.append(f"<{item[:80]}> — reading failed ({type(e).__name__})")
                continue
            if err:
                problems.append(f"<{item[:80]}> — {err}")
            else:
                sources.append((title, item, text))
    return sources, problems


# ------------------------------------------------------------------ Render keep-alive
def start_keepalive():
    """Render web services need an open HTTP port. Render sets PORT; locally this does nothing."""
    port = int(os.environ.get("PORT", 0))
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Verity is awake")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()


# ------------------------------------------------------------------ main handler
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or client.user not in message.mentions:
        return
    started = time.perf_counter()

    ref = None
    if message.reference:
        ref = message.reference.resolved
        if ref is None:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
            except discord.HTTPException:
                ref = None
    ref = ref if isinstance(ref, discord.Message) else None

    mention_text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    ref_text = ref.content.strip() if ref else ""

    attachments = list(message.attachments) + (list(ref.attachments) if ref else [])
    all_text = f"{ref_text}\n{mention_text}"
    urls = extract_urls(all_text)
    other_links = find_unsupported_links(all_text)
    doc_mode = bool(attachments or urls or other_links)

    if not doc_mode and not (ref_text or mention_text):
        return await message.reply("Reply to a message and tag me, type a question, or attach a file / paste a link!")

    fields, footer_note, problems, source_ref, kind = [], "", [], None, "question"

    async with message.channel.typing():
        try:
            if doc_mode:
                kind = "attachment" if attachments else "url"
                question = strip_urls(mention_text) or strip_urls(ref_text) or DEFAULT_QUESTION
                sources, problems = await gather_sources(urls, attachments, other_links)
                source_ref = ", ".join(s[0] for s in sources) or None

                if not sources:
                    log_event(message, kind=kind, query=question, success=False, error="; ".join(problems),
                              latency_ms=int((time.perf_counter() - started) * 1000))
                    e = discord.Embed(title="😕 I couldn't read that", description="\n".join(problems)[:4000],
                                      color=discord.Color.red())
                    e.set_footer(text=f"Supported: {SUPPORTED_SUMMARY}")
                    return await message.reply(embed=e, mention_author=False)

                per = MAX_DOC_CHARS // len(sources)
                truncated = any(len(s[2]) > per for s in sources)
                docs = "\n\n".join(
                    f"===DOCUMENT: {label}===\n{text[:per]}\n===END DOCUMENT===" for label, _, text in sources
                )
                query = f"{question}\n\n{docs}"
                data = await asyncio.to_thread(exa_answer, query, SYSTEM_PROMPT + DOC_PROMPT)
                title = "📄 " + question
                fields.append((
                    "📄 Source" + ("s" if len(sources) > 1 else ""),
                    "\n".join(f"[{(l or u)[:80]}]({u})" if u else f"`{l}`" for l, u, _ in sources),
                ))
                if truncated:
                    footer_note = "Long document: only the start was read"
                top_source = next((u for _, u, _ in sources if u), None)
            else:
                question = ref_text or mention_text
                data = await asyncio.to_thread(exa_answer, question)
                title = "🔎 " + question
                cites = data.get("citations") or []
                top_source = cites[0]["url"] if cites else None
                if cites:
                    fields.append(("🔗 Top source", f"[{(cites[0].get('title') or 'Source')[:200]}]({cites[0]['url']})"))
        except Exception as e:
            log_event(message, kind=kind, query=(ref_text or mention_text), success=False, error=repr(e),
                      latency_ms=int((time.perf_counter() - started) * 1000))
            return await message.reply(f"Oops, the search failed: `{e}`")

    if problems:
        fields.append(("⚠️ Couldn't read", "\n".join(problems)))

    answer = autoformat_code(clean_answer(str(data.get("answer") or "No answer found.")))
    pages = paginate(answer, PAGE_SIZE)
    view = AnswerView(message.author.id, title, pages, extract_code_blocks(answer), fields, footer_note)
    view.message = await message.reply(embed=view.build_embed(), view=view, mention_author=False)

    log_event(message, kind=kind, query=question, source_ref=source_ref, answer=answer, top_source_url=top_source,
              pages=len(pages), success=True, latency_ms=int((time.perf_counter() - started) * 1000))


start_keepalive()
client.run(DISCORD_TOKEN)
