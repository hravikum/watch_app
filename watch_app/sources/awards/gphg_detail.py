"""GPHG watch-detail parser.

Sibling to gphg.py (the listing-page scraper). Fetches a single watch's
detail page from gphg.org and parses its specs into a structured dict
suitable for inserting into the WatchModel table.

Architecture
------------

The page is a Next.js app that ships content as React Flight payloads
embedded in `<script>self.__next_f.push([N, "..."])</script>` tags. The
full payload is the concatenation of every push call.

The payload is a JSON-shaped tree of React elements with three relevant
kinds of nodes:

  - "interned string" definitions: `NN:"text"` lines that the rest of
    the payload references via `$LNN` (or sometimes `$NN`)
  - clearfix blocks containing a `field-label` and a `field-items`,
    where the field-label is either a literal string or an interned-
    string reference, and the field-items.children is one of:
      * a plain string                "100 m"
      * a span with dangerouslySetInnerHTML        Platinum
      * a nested array of strings                  ["Hours, Minutes", " ", ["$","br",null,{}]]
      * a mixed array with empty leading strings   ["", "Polished and..."]
      * a reference to another interned string     ["$","div",null,{...":"$24"}}]
  - grouped detail blocks (`watch-detail-group-movement`,
    `watch-detail-group-functions`) wrap one or more clearfix blocks
    with their own surrounding markup

Strategy: extract every `clearfix` block by tracking bracket depth, then
within each block extract field-label and field-items separately,
recursively pulling every string value out of the field-items subtree.
This handles all variant shapes uniformly.

Field normalizers
-----------------

Each spec has a normalizer that converts the raw string ("ø 40 mm",
"100 m", "85’000 CHF") into the stored form (40.0, 100, 85000). When a
value can't be parsed we leave the field NULL — better to omit than
to record garbage.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


# --- Field normalizers ---


def _parse_diameter_mm(raw: str) -> float | None:
    """'ø 40 mm', '38mm', '38.5 mm', 'Ø 41,5 mm' → float."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*mm", raw, re.IGNORECASE)
    if m is None:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_water_resistance_m(raw: str) -> int | None:
    """'100 m', '50m', '30 metres', '3 ATM' → int meters."""
    s = raw.lower()
    m = re.search(r"(\d+)\s*(?:m\b|metres|meters|m\.|m,)", s)
    if m is not None:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*atm", s)
    if m is not None:
        return int(m.group(1)) * 10
    m = re.search(r"(\d+)\s*bar", s)
    if m is not None:
        return int(m.group(1)) * 10
    return None


def _parse_price_chf(raw: str) -> int | None:
    """'85'000 CHF', '85’000 CHF' (curly apostrophe), '120,000 CHF',
    '85.000 CHF' → 85000.

    GPHG uses the Unicode right-single-quote (U+2019) as a thousands
    separator on real pages. Strip ASCII apostrophes, curly quotes,
    commas, periods, and whitespace from any digit run.
    """
    if "chf" not in raw.lower():
        return None
    m = re.search(r"(\d[\d'’.,\s]*\d|\d+)", raw)
    if m is None:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_movement_caliber(raw: str) -> str | None:
    """The 'Movement' field holds free-text describing the caliber. We
    keep the whole thing as caliber for now. Future refinement could
    extract a caliber number specifically.
    """
    s = raw.strip()
    if not s:
        return None
    return s[:200]


def _parse_movement_type(raw: str) -> str | None:
    """Detect the movement type from a free-text description."""
    s = raw.lower()
    if "self-winding" in s or "self winding" in s or "automatic" in s:
        return "automatic"
    if "hand-wound" in s or "hand wound" in s or "manually wound" in s or "manual winding" in s:
        return "hand-wound"
    if "quartz" in s:
        return "quartz"
    return None


def _parse_frequency_hz(raw: str) -> float | None:
    """'21600 vph', '28800 A/h', '4Hz', '3 Hz' → float Hz.

    1 Hz = 2 vibrations/second = 7200 vph.
    """
    s = raw.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*hz", s)
    if m is not None:
        return float(m.group(1))
    m = re.search(r"(\d[\d',\s]*\d|\d+)\s*(?:vph|v/h|a/h)", s)
    if m is not None:
        digits = re.sub(r"[^\d]", "", m.group(1))
        try:
            return int(digits) / 7200.0
        except ValueError:
            return None
    return None


def _parse_power_reserve_hours(raw: str) -> int | None:
    """'Power reserve: 48 h', '70 hours', '5 days' → int hours."""
    s = raw.lower()
    m = re.search(r"(\d+)\s*days?", s)
    if m is not None:
        return int(m.group(1)) * 24
    m = re.search(r"(\d+)\s*(?:h\b|hour|hours|hrs)", s)
    if m is not None:
        return int(m.group(1))
    return None


def _parse_complications(raw: str) -> list[str] | None:
    """'Hours, Minutes, Small seconds, Date' → ['Hours', 'Minutes', ...]."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts if parts else None


def _parse_limited_edition(raw: str) -> int | None:
    """'25-pieces limited series', '25 -pieces limited series',
    'Limited to 100 pieces' → int count.

    On real GPHG pages the count is rendered separately from the
    "-pieces limited series" suffix; the joined string we receive
    here is something like "25 -pieces limited series" or
    "25-pieces limited series".
    """
    m = re.search(r"(\d+)\s*[- ]?(?:pieces?|examples?)", raw, re.IGNORECASE)
    if m is not None:
        return int(m.group(1))
    m = re.search(r"limited\s+to\s+(\d+)", raw, re.IGNORECASE)
    if m is not None:
        return int(m.group(1))
    return None


# --- Field dispatch ---


#: Maps the GPHG field label (lowercased) to (target field, normalizer).
_FIELD_HANDLERS: dict[str, tuple[str, Any]] = {
    "case material": ("case_material", lambda s: s.strip()[:200] or None),
    "size": ("case_diameter_mm", _parse_diameter_mm),
    "diameter": ("case_diameter_mm", _parse_diameter_mm),
    "water resistance": ("water_resistance_m", _parse_water_resistance_m),
    "functions": ("complications", _parse_complications),
    "complications": ("complications", _parse_complications),
    "price incl. vat": ("price_chf", _parse_price_chf),
    "price": ("price_chf", _parse_price_chf),
    "collection": ("limited_edition_count", _parse_limited_edition),
    "limited edition": ("limited_edition_count", _parse_limited_edition),
    "description": ("description", lambda s: s.strip()[:5000] or None),
}


# --- Flight payload extraction ---


_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\d+,"(.*?)"\]\)',
    re.DOTALL,
)

#: Interned-string definition: `NN:"label or value"` at the start of a
#: logical line in the decoded payload. Used both for resolving label
#: references and for resolving description-style nested references.
_INTERNED_RE = re.compile(r'(?:^|\n)([0-9a-f]+):"((?:[^"\\]|\\.)*?)"', re.DOTALL)

#: Interned-array definition: `NN:[ ... ]` lines. These hold the React
#: subtrees that get pasted in via `$LNN` references.
_INTERNED_ARRAY_RE = re.compile(
    r'(?:^|\n)([0-9a-f]+):(\[.*)',
    re.DOTALL,
)

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
    re.IGNORECASE,
)


def _decode_flight_string(raw: str) -> str | None:
    try:
        return json.loads('"' + raw + '"')
    except json.JSONDecodeError:
        return None


def _extract_flight_payload(html: str) -> str:
    parts: list[str] = []
    for m in _PUSH_RE.finditer(html):
        decoded = _decode_flight_string(m.group(1))
        if decoded is not None:
            parts.append(decoded)
    return "\n".join(parts)


def _build_string_map(payload: str) -> dict[str, str]:
    """Map every `NN:"text"` interned-string definition to its value.

    Both label references (`$L17` → `Water resistance`) and value
    references (`$24` → some description text) live in this map.
    """
    out: dict[str, str] = {}
    for nn, raw_text in _INTERNED_RE.findall(payload):
        # Decode JSON escapes (e.g. \u00e9, \n, \"). The interned strings
        # are JSON-escaped within the Flight-decoded payload.
        try:
            text = json.loads(f'"{raw_text}"')
        except json.JSONDecodeError:
            text = raw_text
        out[nn] = text
    return out


def _build_array_map(payload: str) -> dict[str, str]:
    """Map every `NN:[ ... ]` interned-array definition to the array
    text. Used to expand `$LNN` references that point at full subtrees
    of clearfix blocks (e.g. case material, bracelet strap, buckle).
    """
    out: dict[str, str] = {}
    for nn, after in _INTERNED_ARRAY_RE.findall(payload):
        # Capture everything from the opening `[` up to the matching
        # closing `]` at the same depth.
        arr = _take_balanced(after, "[", "]")
        if arr is not None:
            out[nn] = arr
    return out


def _take_balanced(s: str, open_char: str, close_char: str) -> str | None:
    """Given a string starting with `open_char`, return the substring
    from the start through the matching `close_char` at depth 0,
    inclusive. Tracks string literals so brackets inside JSON strings
    don't confuse the depth count."""
    if not s or s[0] != open_char:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i, c in enumerate(s):
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return s[: i + 1]
    return None


def _extract_strings(text: str) -> list[str]:
    """Extract every double-quoted string literal from a chunk of
    JSON-shaped React markup, skipping React internals and tag-name
    placeholders."""
    pieces: list[str] = []
    for raw in re.findall(r'"((?:[^"\\]|\\.)*)"', text):
        try:
            decoded = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            decoded = raw
        # Filter out React internals and obvious markup.
        if decoded in ("$", "br", "div", "span", "a", "field-items",
                       "field-label", "clearfix", "children", "className",
                       "dangerouslySetInnerHTML", "__html", "null"):
            continue
        if decoded.startswith("$L") or (
            decoded.startswith("$") and len(decoded) <= 5
        ):
            continue
        if not decoded.strip():
            continue
        pieces.append(decoded.strip())
    return pieces


def _resolve_value(
    value_text: str,
    string_map: dict[str, str],
) -> str:
    """Given the raw text of a `field-items.children` (or any nested
    value), return the joined human-readable string, resolving any
    `$NN` references through the string map.

    The resolved-text approach handles every shape we've observed:
    plain strings, dangerouslySetInnerHTML spans, nested arrays,
    mixed arrays with empty leading entries, and references to other
    interned strings.
    """
    out_parts: list[str] = []
    # Find `$NN` references and prefetch their values.
    for ref in re.findall(r'"\$([0-9a-f]+)"', value_text):
        if ref in string_map:
            resolved = string_map[ref]
            if resolved.strip():
                out_parts.append(resolved.strip())
    # Then collect all remaining string literals.
    for s in _extract_strings(value_text):
        out_parts.append(s)
    # Deduplicate while preserving order — references and literals can
    # overlap in some block shapes.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in out_parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return " ".join(deduped).strip()


# --- Block extraction ---


def _expand_l_refs(text: str, array_map: dict[str, str]) -> str:
    """Expand any `$LNN` references that point at full interned-array
    subtrees (most commonly used for clearfix blocks for case material,
    bracelet, buckle, dial finish). After expansion, the text contains
    the inlined subtrees and can be parsed uniformly."""
    out = text
    # Iterate a few times — references can chain.
    for _ in range(3):
        prev = out
        # Match `$LNN` or `"$LNN"` where NN is a hex id.
        out = re.sub(
            r'"?\$L([0-9a-f]+)"?',
            lambda m: array_map.get(m.group(1), m.group(0)),
            out,
        )
        if out == prev:
            break
    return out


_CLEARFIX_START_RE = re.compile(
    r'"className":"clearfix","children":\['
)


def _iter_clearfix_blocks(payload: str) -> list[str]:
    """Find every clearfix block in the payload, returning each as the
    raw substring from the `[` through the matching `]`."""
    blocks: list[str] = []
    for m in _CLEARFIX_START_RE.finditer(payload):
        # m.end() points just past the opening `[`. The full block is
        # `[ ... ]` starting at m.end()-1.
        start = m.end() - 1  # back up to the `[`
        block = _take_balanced(payload[start:], "[", "]")
        if block is not None:
            blocks.append(block)
    return blocks


_LABEL_IN_BLOCK_RE = re.compile(
    r'"className":"field-label","children":(\[[^\]]*\]|"[^"]*"|\$L[0-9a-f]+|"?\$L[0-9a-f]+"?)'
)
_ITEMS_IN_BLOCK_RE = re.compile(
    r'"className":"field-items[^"]*","children":'
)


def _extract_label(block: str, string_map: dict[str, str]) -> str | None:
    """Pull the field label out of a clearfix block. Handles the two
    forms we observe: literal strings (`"Functions"`) and `$LNN`
    references."""
    m = re.search(
        r'"className":"field-label","children":"([^"]+)"',
        block,
    )
    if m is None:
        return None
    raw_label = m.group(1)
    # If it looks like a $LNN reference, resolve it through the string map.
    ref_m = re.fullmatch(r"\$L([0-9a-f]+)", raw_label)
    if ref_m is not None:
        return string_map.get(ref_m.group(1))
    return raw_label


def _extract_items_text(block: str) -> str | None:
    """Pull the field-items children subtree (raw JSON text) out of a
    clearfix block. Returns the substring to be passed to _resolve_value.
    Handles three shapes:
      - plain string:   `"children":"value"`   → returns `"value"`
      - array subtree:  `"children":[...]`     → returns the array text
      - element subtree: `"children":["$","span",...]` → returns the array text
    """
    items_m = _ITEMS_IN_BLOCK_RE.search(block)
    if items_m is None:
        return None
    rest = block[items_m.end():]
    if rest.startswith('"'):
        # Plain string — return the entire literal including quotes so
        # _resolve_value treats it as a quoted string and extracts the
        # value cleanly.
        end = rest.find('"', 1)
        # Skip past escaped quotes inside the string.
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('"', end + 1)
        if end == -1:
            return None
        return rest[: end + 1]
    if rest.startswith("["):
        return _take_balanced(rest, "[", "]")
    return None


# --- Movement extraction ---


_MOVEMENT_GROUP_RE = re.compile(r'"watch-detail-group-movement"')


def _extract_movement_block(
    payload: str,
    array_map: dict[str, str],
) -> str | None:
    """Locate the watch-detail-group-movement block and return the joined
    text of its movement field-items, ready for the movement normalizers
    to consume.

    Movement is special because its field-items.children is a multi-line
    array including `<br>` element sub-arrays. We treat the entire
    subtree as a string-extraction target; the normalizers tolerate
    extra whitespace and stray punctuation.
    """
    # Some pages put the movement group at the top level; others put it
    # inside an interned-array NN block referenced via $LNN. Search both.
    text = payload
    for nn in array_map:
        text = text + "\n" + array_map[nn]

    m = _MOVEMENT_GROUP_RE.search(text)
    if m is None:
        return None
    # From the group label, find the next field-items children subtree.
    rest = text[m.end():]
    items_m = re.search(
        r'"className":"field-items","children":\[',
        rest,
    )
    if items_m is None:
        return None
    start = items_m.end() - 1  # point at the `[`
    block = _take_balanced(rest[start:], "[", "]")
    if block is None:
        return None
    pieces = _extract_strings(block)
    if not pieces:
        return None
    return " ".join(pieces).strip() or None


# --- Main entry point ---


def parse_detail_page(html: str) -> dict[str, Any]:
    """Parse a GPHG watch-detail page into a dict of WatchModel fields.

    Only fields successfully parsed are present in the returned dict.
    Missing labels, unparseable values, and unrecognized labels all
    result in the corresponding key being absent — the caller inserts
    NULL into the DB.
    """
    out: dict[str, Any] = {}

    # og:image lives in <head>, not the Flight payload.
    img = _OG_IMAGE_RE.search(html)
    if img is not None:
        src = img.group(1)
        if src and src.startswith("http"):
            out["image_url"] = src

    payload = _extract_flight_payload(html)
    if not payload:
        return out

    string_map = _build_string_map(payload)
    array_map = _build_array_map(payload)

    # Some clearfix blocks (case material, bracelet strap, buckle, dial
    # finish) are stored as interned arrays referenced from `$LNN`. To
    # process them uniformly we expand those references into the payload.
    expanded = _expand_l_refs(payload, array_map)

    # Walk every clearfix block.
    seen_labels: set[str] = set()
    for block in _iter_clearfix_blocks(expanded):
        label = _extract_label(block, string_map)
        if label is None:
            continue
        label_norm = label.strip().lower()
        if label_norm in seen_labels:
            continue
        seen_labels.add(label_norm)

        items_text = _extract_items_text(block)
        if items_text is None:
            continue
        value = _resolve_value(items_text, string_map)
        if not value:
            continue

        handler = _FIELD_HANDLERS.get(label_norm)
        if handler is None:
            continue
        field_name, normalizer = handler
        try:
            parsed = normalizer(value)
        except Exception:  # noqa: BLE001
            logger.debug("GPHG detail: %s normalizer raised", label_norm)
            continue
        if parsed is not None:
            out[field_name] = parsed

    # Movement is a grouped block, not a clearfix.
    movement_text = _extract_movement_block(payload, array_map)
    if movement_text:
        cal = _parse_movement_caliber(movement_text)
        if cal is not None:
            out["movement_caliber"] = cal
        mt = _parse_movement_type(movement_text)
        if mt is not None:
            out["movement_type"] = mt
        freq = _parse_frequency_hz(movement_text)
        if freq is not None:
            out["frequency_hz"] = freq
        pr = _parse_power_reserve_hours(movement_text)
        if pr is not None:
            out["power_reserve_hours"] = pr

    return out
