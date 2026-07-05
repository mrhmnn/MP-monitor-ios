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
The user repairs and resells iPhones. They only care about phones with a
CHEAP, QUICK repair: a cracked/broken screen, a broken/cracked back cover,
a charging port problem, or cracked camera lens glass. They do NOT care
about phones with expensive, deep damage (motherboard/logic board issues,
won't turn on at all, water damage, iCloud lock, counterfeit/replica
phones) - those are handled by separate rules already, so if the listing
text suggests one of THOSE instead, it's not relevant to this task either.

You will be given a Dutch marketplace listing's title and description.
It's been flagged for one of two reasons: either it contains an ambiguous
term (like "mankement" or "gebrek") that a simple keyword search couldn't
confidently classify, or its title tells buyers to "read the description"
for important details that might contradict what a keyword match alone
would suggest.

Decide: does this listing's actual described condition plausibly match a
cheap screen/back-cover/charging-port/camera-lens repair? Reply with ONLY
a JSON object, no other text:
{"relevant": true or false, "reason": "one short sentence in English"}
"""


def classify_ambiguous_listing(listing_text: str, model: str) -> AiVerdict:
    """
    Send one ambiguous listing to Haiku for a relevance judgment.
    Fails safe: if anything goes wrong, treat it as NOT relevant rather than
    risk spamming a notification for something we couldn't actually verify.
    """
    client = _get_client()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": listing_text[:1500]}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences, just in case
        raw = raw.replace("```json", "").replace("```", "").strip()

        import json

        parsed = json.loads(raw)
        return AiVerdict(relevant=bool(parsed["relevant"]), reason=parsed.get("reason", ""))

    except Exception as exc:  # noqa: BLE001 - we want to fail safe on ANY error here
        logger.warning("AI classification failed, defaulting to not-relevant: %s", exc)
        return AiVerdict(relevant=False, reason=f"classification error: {exc}")
