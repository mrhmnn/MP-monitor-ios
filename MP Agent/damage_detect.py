"""
damage_detect.py

Standalone broad damage-term detector (single-word/substring based), added
July 12, 2026. NOT wired into the live filter pipeline: filters.py's
evaluate_listing() remains the production decision path. This module's
DAMAGE list deliberately includes terms the live pipeline hard-excludes
(waterschade, icloud, moederbord, gestolen, blacklist) - it answers
"does this text mention damage at all?", not "is this a profitable flip?".
Useful for market analysis / recall testing; do not plug into main.py
without reconciling with config.yaml's hard_excludes.
"""

import re

DAMAGE = ["barst","breuk","gebroken","broken","kapot","scheur","beschadig","schade",
"defect","crack","deuk","kras","buts","stuk","spinnenweb","glasbreuk","dode pixel",
"groene lijn","groene streep","inbranding","burn in","waterschade","vocht","nat geweest",
"gevallen","valschade","moederbord","geen beeld","bootloop","oververhit","opgezwollen",
"bolle batterij","accu defect","camera kapot","laadpoort","face id defect","trilmotor",
"voor onderdelen","onderdelen","sloop","donor","niet werkend","als is","zoals gezien",
"te repareren","reparatie","icloud","blacklist","gestolen"]

NEG = ("geen","niet","zonder","nooit")

def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def is_damaged(title, description=""):
    t, d = _norm(title), _norm(description)
    hits = []

    # TITLE: negation-aware
    for w in DAMAGE:
        i = t.find(w)
        if i == -1:
            continue
        before = t[:i].split()[-4:]
        after = t[i+len(w):i+len(w)+5]
        if any(x in NEG for x in before) or after.startswith(("vrij","loos")):
            continue          # "geen barst", "krasvrij"
        hits.append(w)

    # DESCRIPTION: plain match, no negation (unchanged behaviour)
    hits += [w for w in DAMAGE if w in d]

    return (bool(hits), sorted(set(hits)))
