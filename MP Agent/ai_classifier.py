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

import base64
import json

import httpx
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

- SCREEN: cracks/breaks (barst/breuk/scheur/gebarsten), but ALSO panel
  defects fixed by the exact same screen swap - spots/stains in the display
  (vlekken/vlekjes in het beeld), lines/stripes (strepen/lijnen, groene
  lijn), burn-in (inbranding), dead pixels, touch not responding. A working
  phone with display blemishes is a screen repair, and exactly what the
  user wants. NOT included: mere surface scratches on intact glass - see
  the scratch rule below.
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
  with downplayed screen or back damage is the IDEAL buy. (The sole
  exception is the surface-scratch rule below: a scratch is not a
  downplayed crack, it is a different, non-repairable category.)
- A battery HEALTH percentage ("batterijconditie 93%", "accu 85%") is a
  normal spec, NOT damage - never treat it as a defect or a reason to
  reject. The 17-gen battery/charging exclusion applies only when the
  described DEFECT itself is the battery or charging.
- A damaged SCREEN PROTECTOR (screenprotector, beschermglas, privacy
  glass) is a removable accessory, NOT screen damage. If only the
  protector is damaged and the phone itself is fine -> not relevant.
- SURFACE SCRATCHES on the screen or back glass ALONE (krasjes/krassen op
  het scherm/glas, "gebruikssporen", "lichte slijtage") are cosmetic wear,
  NOT a repairable defect: the glass is intact and the display works, so
  there is nothing to swap, and the seller prices it as a normal working
  phone - no margin to capture. This is the ONE place severity matters,
  because a scratch is by definition surface-only. Scratches count ONLY
  when a REAL defect is also present - a crack/breuk/scheur, any panel
  defect from the SCREEN list, back-cover damage, a charging/battery fault,
  etc. Never downgrade a crack or a panel defect to "just a scratch": those
  always qualify no matter how softly the seller words them.
- Damage that was ALREADY REPAIRED ("scherm vervangen", "gerepareerd",
  "onder garantie hersteld") is not a defect - the phone works and needs
  no repair. Relevant only if a CURRENT, unrepaired defect remains.
- DAMAGE ASSERTED BUT NOT SPECIFIED -> relevant: true. If the listing
  states the phone is damaged/broken (schade, beschadigd, gebroken,
  kapot, defect) but never says WHICH part, do NOT reject for lack of
  detail. Marktplaats truncates descriptions to ~230 characters, so the
  sentence naming the part is very often simply cut off, and sellers
  routinely point at photos instead ("zie foto's"). Missing information
  is a platform artefact, not evidence of expensive damage - never
  reason that unspecified damage is "likely" a board fault or "probably"
  a write-off. Most damaged iPhones on this site have exactly the cheap
  screen or back-glass damage the user wants, so an unspecified defect
  is far more likely to be in-category than out. This applies on ALL
  models 14-17: with no named defect there is nothing for the 17-gen
  battery/charging exclusion to apply to, so it does NOT apply.
  This rule needs damage to be ASSERTED. It does not apply when the
  listing simply says nothing about condition, nor when it describes a
  working phone - those are still rejects.
- OFFERING THE PHONE "VOOR ONDERDELEN" / "voor reparatie" / "voor iemand
  die handig is" / "sloop" IS such an assertion, and falls under the rule
  above even though no part is named. Sellers use this framing to set
  buyer expectations and price low - it is not a diagnosis. Do NOT infer
  hidden deep damage from the phrase alone: if no board / water / iCloud /
  counterfeit problem is actually NAMED, treat it as an unknown-but-
  probably-fixable defect and answer relevant: true. A phone the seller
  has already written off is precisely where the margin is. This holds on
  ALL models 14-17, not only the expensive ones - the phrase is evidence
  about the seller's expectations, not about the repair bill.
  Two things this does NOT cover: a listing selling a PART or accessory
  rather than a whole phone ("iPhone 16 Pro onderdeel - achter camera", a
  loose screen, a bare housing), and a repair shop advertising its
  services. Both are still rejects.
- PHOTOS: images are attached only when the seller pointed at them instead
  of naming the damage ("zie foto's"). When they are there, read them and
  say what you can see broken. When they are not, judge on the text alone -
  never ask for photos or complain that none were provided. Sellers constantly
  write "zie foto's" and name nothing, and on this marketplace the picture
  IS the description. If you can see a crack, a shattered panel, a damaged
  back, lines or spots on the display, judge on that and name what you saw.
  A photo showing real damage OVERRIDES a vague or reassuring text. Note
  what photos cannot settle: water damage, board faults, iCloud lock and
  Face ID are invisible, so never infer them from an image - and a phone
  that merely LOOKS clean is not proof it works, since the defect may be
  internal or on a face not pictured. Absence of visible damage is
  therefore not evidence against a defect the seller has asserted.
- Reject only when every described defect falls outside the categories,
  or there is no actual defect at all (seller just selling a fine phone).

Examples:
- "iPhone 16 Pro Max, 93% batterij. Achterkant lichte schade, niet
  storend. Twee kleine vlekjes in het beeld." -> relevant: true (display
  spots = screen repair; back damage = back cover; 93% is health, not
  a defect).
- "iPhone 15, barstje in de hoek, werkt perfect" -> relevant: true (a
  crack is a screen swap, however small - never a "just a scratch").
- "iPhone 15 Pro, alles werkt, enkele lichte krasjes op het scherm, met
  screenprotector nauwelijks zichtbaar; klein beschadigd puntje aan de
  zijkant." -> relevant: false (surface scratches on intact glass + a
  cosmetic side nick = wear on a working phone, no repairable defect).
- "iPhone 15, krasjes op het scherm en de achterkant is gebarsten." ->
  relevant: true (the cracked back is a cheap swap; the scratches neither
  qualify nor disqualify it).
- "iPhone 14 werkt niet meer na in het water te zijn gevallen" ->
  relevant: false (water damage).
- "iPhone 14 Pro Max, klein barstje in de screenprotector, toestel
  zelf zonder schade" -> relevant: false (protector is an accessory,
  the phone's own screen is fine).
- "iPhone 15 Pro, scherm onlangs vervangen vanwege defect, werkt nu
  perfect" -> relevant: false (already repaired - no current defect
  remains).
- "Werkende iphone 15. Toestel is beschadigd, zie foto's voor de
  staat." -> relevant: true (damage is asserted; the description is
  truncated and points at photos, so the part is unnamed - that is
  missing information, NOT evidence of expensive damage. A working
  phone that is nonetheless damaged is the ideal buy).
- "Defecte iPhone 15 voor onderdelen, scherm en behuizing intact." ->
  relevant: true (parts framing is not evidence of a board fault; no
  deep-fault signal is named, so this is an unknown-but-probably-fixable
  defect - and the intact screen and housing make a cheap repair more
  likely, not less).
- "iPhone 16 Pro Max voor onderdelen. Waarschijnlijk iCloud-slot, kreeg
  hem zo." -> relevant: false (damage is unspecified, but a hard-exclude
  signal IS named - iCloud lock. The unspecified-damage rule never
  overrides an explicitly stated deep fault).

You will be given a Dutch marketplace listing's title and description.
It's been flagged for one of three reasons: it contains an ambiguous term
(like "mankement" or "schade") that a simple keyword search couldn't
confidently classify; its title tells buyers to "read the description"
for important details that might contradict what a keyword match alone
would suggest; or it names a target iPhone model and came from a
damage-focused search but matched no known damage keyword - meaning the
seller described the damage in their own words and you need to judge
whether it plausibly falls in the cheap-repair categories.

Two things are asked of you, and the SECOND matters more:

1. "relevant": does the described condition plausibly match a cheap
   screen / back-cover / charging-port / battery / camera-lens repair?
   "Plausibly" - you are not confirming the defect, only judging whether
   it could be one of these. If damage is asserted but the part is never
   named, the answer is yes.

2. "reason": SAY WHAT IS BROKEN, in one short sentence. This string is
   shown to the user in the alert he reads on his phone, before he opens
   the ad - it is the whole point of this call. Name the part and the
   fault ("cracked back glass, screen intact", "won't power on, no cause
   given", "screen has green lines"). Lead with the damage.
   If the listing names no defect at all, say what the listing IS
   instead, briefly ("sealed new phone, no defect stated", "shop
   advertisement", "phone case only") - never write "cannot assess" or
   "no information", and never explain your own reasoning about the
   categories above. He is reading this to decide whether to open the
   ad, not to audit your verdict.

Reply with ONLY a JSON object, no other text:
{"relevant": true or false, "reason": "one short sentence in English"}
"""


def _parse_first_json_object(raw: str) -> dict:
    """Parse the FIRST JSON object in `raw`, ignoring any prose the model
    appended after it.

    A plain json.loads() rejects the entire response when the model adds a
    trailing sentence after valid JSON - it raises "Extra data: line N".
    That is a recoverable formatting slip, not an unusable answer, but it
    was being buried as a reject. Real production miss 2026-07-24: "iPhone
    15 Pro 128GB Titanium Blauw - Oplaadpunt defect" - a charging-port
    repair, one of the cheapest jobs there is, on a model with a EUR 472
    verkocht median - was silently dropped this way.

    raw_decode() reads one JSON value and reports how far it got, so the
    trailing prose is simply ignored. Genuinely unparseable output still
    raises JSONDecodeError and keeps the existing fail-closed behaviour.
    """
    start = raw.find("{")
    if start == -1:
        raise json.JSONDecodeError("no JSON object in response", raw, 0)
    parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("response JSON is not an object", raw, 0)
    return parsed


# Appended to SYSTEM_PROMPT only for the models in config's
# `high_value_models` (2026-07-28). Those three models alerted ZERO times in
# four days against ~12 expected: the base prompt's cheap-repair-only rule
# rejects nearly everything that actually gets listed on them, because the
# defects people list a €650-1050 phone with are Face ID, camera modules and
# bare "voor onderdelen". Milad's explicit call was to trade precision for
# recall here - three alerts with two duds beats a silent channel - so this
# block widens the accept categories rather than loosening severity judgment.
# It deliberately does NOT touch the surface-scratch rule (that was his own
# 07-24 request after near-mint 15 Pros kept alerting) and does NOT override
# the hard excludes.
HIGH_VALUE_SUFFIX = """

OVERRIDE FOR THIS LISTING - it is one of the user's HIGHEST-VALUE models
(iPhone 16 Pro Max / 17 Pro / 17 Pro Max, resale €650-1050). The resale
margin here is wide enough to absorb a repair that would not be worth it on
a cheaper phone, and the user can also simply resell the phone AS-IS with
the defect disclosed. On this listing only:

- FACE ID broken, CAMERA MODULE faults, and any similar "expensive repair"
  defect ARE relevant. Do not reject them for being costly. A phone that is
  otherwise fully working with one scary-sounding defect sells at a steep
  discount precisely because most buyers avoid it - that discount is the
  opportunity.
- BATTERY and CHARGING PORT faults are relevant on 17-gen too (the base
  prompt's "17-gen battery/charging is too expensive" exclusion does NOT
  apply to these models).
- (The "voor onderdelen" / "voor reparatie" rule is no longer listed here:
  as of 2026-08-19 it lives in the base prompt and applies to every model
  14-17. It was never a value-tier judgment - the phrase says what the
  seller expects, not what the repair costs. Nothing changes for these
  models; the rule simply is not exclusive to them anymore.)
- Still reject: water damage, motherboard/logic-board failure, iCloud lock,
  counterfeit/replica, and listings with NO defect at all. Those are real
  write-offs, not priced-in risk.
- The surface-scratch rule above still applies unchanged: scratches on
  intact glass with no other defect are still not a target.
"""


# Marktplaats serves each photo at a size code in the URL. Haiku bills images
# at roughly (width * height) / 750 tokens, so this choice is the whole image
# cost: "86" is 900x1600 (~1920 tokens/image), "84" is 498x885 (~588). At three
# photos a call that is 5760 vs 1764 tokens - measured 2026-08-19, and at
# ~45 ambiguous listings/day the difference is about EUR 5/month. 498px wide is
# still plenty to see a cracked screen or back, which is all this pass judges.
_IMAGE_SIZE_CODE = "84"


def _image_blocks(image_urls: list[str] | None, limit: int) -> list[dict]:
    """Download listing photos and return them as Anthropic image blocks.

    Half of these listings describe the damage as "zie foto's" and say nothing
    else - the picture IS the description. Judging those on text alone is
    guessing, which is what produced a run of "no specific defect named"
    rejects on phones that were plainly cracked in the photo.

    Best-effort by design: a photo that will not download is skipped rather
    than failing the listing, because a text-only verdict is still far better
    than no verdict. Marktplaats serves these from images.marktplaats.com with
    a size rule in the URL; "$_86" is a large-but-not-huge variant, enough to
    see a crack without paying for a full-resolution upload.
    """
    if not image_urls:
        return []

    blocks: list[dict] = []
    for url in image_urls[:limit]:
        if url.startswith("//"):
            url = "https:" + url
        url = url.replace("$_#.jpg", "$_" + _IMAGE_SIZE_CODE + ".jpg")
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            if not media_type.startswith("image/"):
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(resp.content).decode("ascii"),
                },
            })
        except Exception as exc:  # noqa: BLE001 - a missing photo must not sink the listing
            logger.debug("could not attach listing image %s: %s", url, exc)
    return blocks


def classify_ambiguous_listing(
    listing_text: str,
    model: str,
    high_value: bool = False,
    image_urls: list[str] | None = None,
    max_images: int = 3,
) -> AiVerdict:
    """
    Ask Haiku what the damage on this listing actually is.

    NOT A GATE (2026-08-20, Milad's call: "i only want the ai review for
    context in the alerts to specify the damage but alert me either way,
    don't make the ai review the decider"). main.py alerts on every listing
    that reaches this function regardless of what comes back; `relevant` is
    kept only so the reason string can be labelled, and `reason` is the
    product - it is what gets written into the Telegram alert so he knows
    what is broken before opening the ad.

    The real gate is filters.py: target model, hard excludes (iCloud, water,
    board, counterfeit), bulk lots, buyer ads, business sellers, distance.
    A listing this function sees has already passed all of that.

    Photos are attached ONLY when the caller passes them, and main.py passes
    them only when the listing points at its pictures instead of naming the
    damage ("zie foto's"). They cost ~588 tokens each and are useless when
    the seller already said what is broken.
    """
    try:
        # Inside the try on purpose: a missing ANTHROPIC_API_KEY used to raise
        # RuntimeError out of here, and nothing in main.py catches it - the
        # whole scan died mid-loop, silently discarding every match already
        # collected in that run (they were marked seen, so they never alerted
        # again either). Now it degrades to a transient error: the listing is
        # left unseen and retried once the key works.
        client = _get_client()
        system = SYSTEM_PROMPT + HIGH_VALUE_SUFFIX if high_value else SYSTEM_PROMPT
        content: list[dict] = _image_blocks(image_urls, max_images)
        content.append({"type": "text", "text": listing_text[:1500]})
        response = client.messages.create(
            model=model,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences, just in case
        raw = raw.replace("```json", "").replace("```", "").strip()

        parsed = _parse_first_json_object(raw)
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
