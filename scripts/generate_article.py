"""
Fetches trending tech news from RSS feeds, picks one story at random, and asks
an AI model to draft an original blog post about it (never copying the source).

Falls back across providers in order: Gemini -> OpenRouter -> Groq.
The first provider that succeeds wins; if all three fail, the run fails loudly
so the GitHub Actions run shows a red X instead of silently doing nothing.
"""

import json
import os
import random
import re
import sys
import time
from urllib.request import Request, urlopen

import feedparser

FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "http://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/rss",
]

SYSTEM = (
    "You are a tech blog writer for a news-focused blog. "
    "You never reproduce source text verbatim or closely paraphrase it."
)


def fetch_stories():
    stories = []
    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:10]:
                stories.append(
                    {
                        "title": entry.get("title", ""),
                        "link": entry.get("link", ""),
                        "summary": (entry.get("summary", "") or "")[:500],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
    return stories


def build_prompt(story):
    return (
        f'Write an original tech news blog post inspired by this headline: "{story["title"]}" '
        f'(source: {story["link"]}, snippet: {story["summary"]}). '
        "Do not copy or closely paraphrase the source; write fresh analysis, context, and "
        "commentary in your own voice for a tech-savvy audience. "
        "Return ONLY a raw JSON object (no markdown code fences) with keys: "
        '"slug" (kebab-case, url-safe), "title" (concise, SEO-friendly), '
        '"description" (one sentence summary), and "body" '
        "(the full Markdown body, 600-900 words, using ## headings where useful)."
    )


def _post_json(url, payload, headers, timeout=60):
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def call_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": SYSTEM + "\n\n" + prompt}]}],
        "generationConfig": {"temperature": 0.8},
    }
    data = _post_json(url, payload, {"Content-Type": "application/json"})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_openrouter(prompt):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = _post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
    return data["choices"][0]["message"]["content"]


def call_groq(prompt):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = _post_json("https://api.groq.com/openai/v1/chat/completions", payload, headers)
    return data["choices"][0]["message"]["content"]


PROVIDERS = [
    ("gemini", call_gemini),
    ("openrouter", call_openrouter),
    ("groq", call_groq),
]


def draft_with_fallback(prompt):
    last_err = None
    for name, fn in PROVIDERS:
        try:
            print(f"Trying provider: {name}")
            text = fn(prompt)
            print(f"Provider succeeded: {name}")
            return text, name
        except Exception as exc:  # noqa: BLE001
            print(f"Provider failed: {name} -> {exc}", file=sys.stderr)
            last_err = exc
    raise RuntimeError(f"All providers failed. Last error: {last_err}")


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    return json.loads(text[start : end + 1])


def slugify(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:80] or "post"


def main():
    stories = fetch_stories()
    if not stories:
        print("No stories fetched from any feed, exiting.", file=sys.stderr)
        sys.exit(1)

    story = random.choice(stories)
    prompt = build_prompt(story)
    raw_text, provider = draft_with_fallback(prompt)
    post = extract_json(raw_text)

    slug = slugify(post.get("slug") or post["title"])
    pub_date = time.strftime("%Y-%m-%d")
    title = post["title"].replace('"', '\\"')
    description = post["description"].replace('"', '\\"')

    frontmatter = (
        "---\n"
        f'title: "{title}"\n'
        f'description: "{description}"\n'
        f'pubDate: "{pub_date}"\n'
        "---\n\n"
    )
    content = frontmatter + post["body"]

    os.makedirs("src/content/blog", exist_ok=True)
    path = f"src/content/blog/{slug}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Wrote {path} using provider: {provider}")

    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"POST_SLUG={slug}\n")
            f.write(f"POST_PROVIDER={provider}\n")


if __name__ == "__main__":
    main()
