"""
ai_classifier.py

Handles the small minority of listings that plain keyword filtering can't
resolve on its own (mainly: "mankement" mentioned without a clear negation,
where we need to actually understand what the defect is).

Deliberately isolated from the rest of the app - the prompt lives here and
nowhere else, so it's easy to tune without touching filtering/scraping logic.

Uses Haiku, not Sonnet: this is a cheap, high-volume classification task,
not something that needs frontier reasoning. See project notes on cost.
"""

import json
import logging
import os
from dataclasses import dataclass

from anthropic import Anthropic

logger = logging.getLogger(__name__)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in."
            )
        _client = Anthropic(api_key=api_key)
    return _client


@dataclass
class AiVerdict:
    relevant: bool
    reason: str


SYSTEM_PROMPT = """You are a filter for a secondhand phone marketplace monitor.
The user repairs and resells iPhones (models 14-17). They care about phones
whose damage is a CHEAP, QUICK repair. That means ANY of these categories:

- SCREEN: cracks/breaks, but ALSO panel defects fixed by the exact same
  screen swap - spots/stains in the display (vlekken/vlekjes in het beeld),
  lines/stripes (strepen/lijnen, groene lijn), burn-in (inbranding), dead
  pixels, touch not responding. A working phone with display blemishes is
  a screen repair, and exactly what the user wants.
- BACK COVER: cracked, broken, or cosmetically damaged back glass -
  including damage the seller calls light or "niet storend". Cosmetic back
  damage still lowers the buy price and is a cheap swap on base/Plus models.
- CHARGING PORT problems (14-16 gen).
- BATTERY worn/defect (14-16 gen only - for 17-gen phones, battery and
  charging repairs are expensive, treat those as NOT relevant).
- CAMERA LENS GLASS cracked (the glue-on outer glass, not the module).

They do NOT care about expensive, deep damage: motherboard/logic board
issues, water damage, Face ID broken, iCloud lock, counterfeit/replica
phones. If the listing suggests one of THOSE as the main problem, it's
not relevant.

A phone that won't turn on ("gaat niet (meer) aan", "doet het niet") IS
relevant: that's usually a dead battery or screen, both cheap repairs -
UNLESS the seller also mentions water damage, board damage, or a failed
repair attempt, in which case it's not.

DECISION RULE - apply it mechanically:
- If ANY described defect falls in the categories above -> relevant: true.
- All models 14 through 17 are equally wanted targets. Do NOT reason
  about model year, resale value, cost-benefit, or whether a repair
  "justifies the cost" - profitability is calculated elsewhere, it is
  NOT your job. Your only job is matching the defect to a category.
- Sellers systematically downplay damage ("lichte schade", "kleine
  vlekjes", "niet storend", "verder werkt alles perfect"). Judge the
  damage CATEGORY, not the severity wording - a mostly-working phone
  with downplayed screen or back damage is the IDEAL buy.
- A battery HEALTH percentage ("batterijconditie 93%", "accu 85%") is a
  normal spec, NOT damage - never treat it as a defect or a reason to
  reject. The 17-gen battery/charging exclusion applies only when the
  described DEFECT itself is the battery or charging.
- A damaged SCREEN PROTECTOR (screenprotector, beschermglas, privacy
  glass) is a removable accessory, NOT screen damage. If only the
  protector is damaged and the phone itself is fine -> not relevant.
- Damage that was ALREADY REPAIRED ("scherm vervangen", "gerepareerd",
  "onder garantie hersteld") is not a defect - the phone works and needs
  no repair. Relevant only if a CURRENT, unrepaired defect remains.
- Reject only when every described defect falls outside the categories,
  or there is no actual defect at all (seller just selling a fine phone).

Examples:
- "iPhone 16 Pro Max, 93% batterij. Achterkant lichte schade, niet
  storend. Twee kleine vlekjes in het beeld." -> relevant: true (display
  spots = screen repair; back damage = back cover; 93% is health, not
  a defect).
- "iPhone 15, barstje in de hoek, werkt perfect" -> relevant: true.
- "iPhone 14 werkt niet meer na in het water te zijn gevallen" ->
  relevant: false (water damage).
- "iPhone 15 Pro in nette staat, geen gebreken" -> relevant: false (no
  defect at all).
- "iPhone 14 Pro Max, klein barstje in de screenprotector, toestel
  zelf zonder schade" -> relevant: false (protector is an accessory,
  the phone's own screen is fine).
- "iPhone 15 Pro, scherm onlangs vervangen vanwege defect, werkt nu
  perfect" -> relevant: false (already repaired - no current defect
  remains).

You will be given a Dutch marketplace listing's title and description.
It's been flagged for one of three reasons: it contains an ambiguous term
(like "mankement" or "schade") that a simple keyword search couldn't
confidently classify; its title tells buyers to "read the description"
for important details that might contradict what a keyword match alone
would suggest; or it names a target iPhone model and came from a
damage-focused search but matched no known damage keyword - meaning the
seller described the damage in their own words and you need to judge
whether it plausibly falls in the cheap-repair categories.

Decide: does this listing's actual described condition plausibly match a
cheap screen/back-cover/charging-port/battery/camera-lens repair? Reply
with ONLY a JSON object, no other text:
{"relevant": true or false, "reason": "one short sentence in English"}
"""


def classify_ambiguous_listing(listing_text: str, model: str) -> AiVerdict:
    """
    Send one ambiguous listing to Haiku for a relevance judgment.
    Fails safe: if anything goes wrong, treat it as NOT relevant rather than
    risk spamming a notification for something we couldn't actually verify.
    """
    try:
        # Inside the try on purpose: a missing ANTHROPIC_API_KEY used to raise
        # RuntimeError out of here, and nothing in main.py catches it - the
        # whole scan died mid-loop, silently discarding every match already
        # collected in that run (they were marked seen, so they never alerted
        # again either). Now it degrades to a transient error: the listing is
        # left unseen and retried once the key works.
        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": listing_text[:1500]}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences, just in case
        raw = raw.replace("```json", "").replace("```", "").strip()

        parsed = json.loads(raw)
        return AiVerdict(relevant=bool(parsed["relevant"]), reason=parsed.get("reason", ""))

    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        # PERMANENT failure: the call succeeded, the model just didn't return
        # usable JSON for this text. Deliberately NOT reported as a
        # "classification error" - main.py retries those forever, and a
        # response this listing's text reliably produces would be re-fetched
        # and re-billed every run (~180/day) while never alerting. Bury it
        # as a normal reject instead; the reason string makes it greppable.
        logger.warning("AI returned unparseable output, treating as reject: %s", exc)
        return AiVerdict(relevant=False, reason=f"unparseable AI response: {exc}")

    except Exception as exc:  # noqa: BLE001 - fail safe on any TRANSIENT error
        # API/network failure (529 overloaded, timeout, connection reset).
        # main.py leaves these unseen so the next run retries them.
        logger.warning("AI classification failed, defaulting to not-relevant: %s", exc)
        return AiVerdict(relevant=False, reason=f"classification error: {exc}")
