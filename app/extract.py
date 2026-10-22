"""Stage 2: extract candidate movie titles from text (+ images) using the LLM."""
import base64
import json
import os

from openai import OpenAI

from . import config

_client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "dummy")

_SYSTEM = (
    "You identify MOVIE titles that are the MAIN SUBJECT of a social-media post. "
    "Return ONLY real feature films (not TV series, actors, or directors). "
    "Extract only the films that are the post's actual entries — e.g. the items of a "
    "ranked/numbered/bulleted list, or the films the post explicitly recommends or reviews. "
    "EXCLUDE any film that is only mentioned in passing, used as a comparison or example, "
    "cited as an influence/inspiration, or referenced inside the description of another film. "
    "Only include a movie if it is explicitly named in the provided text or clearly "
    "visible in an attached image. NEVER guess, infer, or invent titles, and never "
    "pad the list with famous movies that are not actually the subject. "
    "Prefer the canonical English title. Respond with a single JSON object of the form "
    '{"movies": [{"title": "<title>", "year": <release year or null>, '
    '"confidence": <0..1>}]}. If the input is empty or contains no movie references, '
    'return {"movies": []}.'
)


_LIST_SYSTEM = (
    "You extract MOVIE titles from a structured list page (e.g. a Wikipedia list or "
    "awards table). Return EVERY feature film that is an entry of the lists/tables in "
    "the provided text — do not skip rows and do not stop early. Ignore people, countries, "
    "TV series and films mentioned only in prose. Rows in tables may omit a repeated "
    "first cell (e.g. the year) — inherit it from the row above. Use the English title "
    "column when present, otherwise the original title. The year is the release or award "
    "year shown in the row. Respond with a single JSON object of the form "
    '{"movies": [{"title": "<title>", "year": <year or null>, "confidence": <0..1>}]}. '
    'If there are no films, return {"movies": []}.'
)
_LIST_CHUNK_CHARS = 6000


def extract_titles(source_text: str, image_paths: list[str], list_mode: bool = False) -> list[dict]:
    # Never call the model with no evidence — that is where hallucinated lists come from.
    if not (source_text or "").strip() and not image_paths:
        print("[extract] no text or images extracted; skipping LLM")
        return []

    if list_mode:
        results = []
        for chunk in _chunk_lines(source_text, _LIST_CHUNK_CHARS):
            results.extend(_extract_list_chunk(chunk))
        return _dedupe(results)

    # The model caps images per request (gemini flash = 10), so batch them.
    batch = 6
    if not image_paths:
        results = _extract_once(source_text, [])
    else:
        results = []
        for i in range(0, len(image_paths), batch):
            chunk = image_paths[i:i + batch]
            results.extend(_extract_once(source_text if i == 0 else "", chunk))
    return _dedupe(results)


def _dedupe(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for m in results:
        key = (m["title"].strip().lower(), m.get("year"))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _chunk_lines(text: str, limit: int) -> list[str]:
    """Split on line boundaries so long tables stay within the model's output budget."""
    lines = (text or "").splitlines()
    header = lines[0] if lines else ""
    chunks, cur = [], []
    size = 0
    for ln in lines:
        if cur and size + len(ln) > limit:
            chunks.append("\n".join(cur))
            # Repeat the page header and the previous line so carried-over cells (year) survive.
            cur, size = [header, cur[-1]], len(header) + len(cur[-1])
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _extract_list_chunk(text: str) -> list[dict]:
    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _LIST_SYSTEM},
                {"role": "user", "content": f"List page content:\n---\n{text}"},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
    except Exception as exc:  # noqa: BLE001
        print(f"[extract] list extraction failed: {exc}")
        raw = "{}"
    return _parse(raw)


def _extract_once(source_text: str, image_paths: list[str]) -> list[dict]:
    content: list[dict] = [{
        "type": "text",
        "text": (
            "List the movies that are the main entries/subject of the following post "
            "(e.g. the films in its list or the ones it recommends). Ignore films only "
            "mentioned in passing or as comparisons. Consider the caption/transcript text "
            "AND any titles visible in the attached images (posters, screenshots, on-screen "
            "text).\n\n---\n"
            f"{source_text or '(no text extracted)'}"
        ),
    }]
    for path in image_paths:
        data_uri = _image_data_uri(path)
        if data_uri:
            content.append({"type": "image_url", "image_url": {"url": data_uri}})

    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
    except Exception as exc:  # noqa: BLE001 - some proxies reject response_format/vision
        print(f"[extract] structured call failed ({exc}); retrying as plain text")
        raw = _fallback_text_only(source_text)

    return _parse(raw)


def _fallback_text_only(source_text: str) -> str:
    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Post text:\n{source_text}\n\nReturn JSON only."},
            ],
            temperature=0.1,
        )
        return resp.choices[0].message.content or "{}"
    except Exception as exc:  # noqa: BLE001
        print(f"[extract] fallback failed: {exc}")
        return "{}"


def _parse(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []

    movies = data.get("movies", []) if isinstance(data, dict) else []
    out = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        title = (m.get("title") or "").strip()
        if not title:
            continue
        year = m.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        out.append({
            "title": title,
            "year": year,
            "confidence": float(m.get("confidence", 0.0) or 0.0),
        })
    return out


def suggest_list_name(source_text: str, titles: list[str] | None = None) -> str:
    """Refine the post's headline into a concise movie-list title."""
    text = (source_text or "").strip()
    if not text and not titles:
        return "Collecta list"
    headline = text.splitlines()[0].strip() if text else ""
    user = (
        "Create a title for a movie collection based on this post.\n"
        f"Post headline: {headline or '(none)'}\n"
        + (f"Movies in it: {', '.join(titles[:15])}\n" if titles else "")
        + "Rules: Title Case, 3-8 words, specific and descriptive, no quotes, no trailing "
          "punctuation. Do NOT return generic words like 'Top', 'Movies', or 'List'. "
          "Base it on the headline when it is meaningful. Return ONLY the title."
    )
    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You name movie collections concisely and specifically."},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        choices = resp.choices or []
        content = (choices[0].message.content or "") if choices else ""
        name = content.strip().strip('"')
        name = name.splitlines()[-1].strip().strip('"').rstrip(".").strip() if name else ""
        if len(name.split()) >= 2 and name.lower() not in ("top movies", "movie list"):
            return name[:80]
    except Exception as exc:  # noqa: BLE001
        print(f"[extract] list-name suggestion failed: {exc}")
    return _fallback_name(headline or text)


def _fallback_name(text: str) -> str:
    first = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return first[:80] or "Collecta list"


def _image_data_uri(path: str) -> str | None:
    # Downscale/compress so multi-image requests stay under the model's body-size limit.
    try:
        import io

        from PIL import Image

        img = Image.open(path).convert("RGB")
        img.thumbnail((768, 768))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001 - fall back to the raw file
        try:
            ext = os.path.splitext(path)[1].lower().lstrip(".") or "jpeg"
            if ext == "jpg":
                ext = "jpeg"
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            return f"data:image/{ext};base64,{b64}"
        except OSError:
            return None
