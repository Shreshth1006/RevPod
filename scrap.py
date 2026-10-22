"""
TOI homepage hero-slot tracker (CMS feed, no scraping).

Tracks fixed positions in the feed, independent of content:
    sections[id=topstories].items[tn=col_2].items[9|10|11]  ->  slots 10, 11, 12
Section and column are located by id/tn (their array index can shift);
inside the column the array index is pinned. Whatever sits there is tracked:
story, DFP ad slot, or widget.

Every run:
  1. GET webhomepagefeedspadata.cms?feedtype=sjson
  2. Rebuild the hero column card-by-card in on-screen order (stories + ad slots)
  3. For each tracked slot: store headline / url / image, compare with last run
  4. On any change, write a change event: what went out, what came in,
     where the outgoing story went, where the incoming one came from,
     and how long the outgoing story had held the slot

Storage: Supabase (SUPABASE_URL + SUPABASE_KEY), else local files in ./hero_data

  python hero_scraper.py             # normal run
  python hero_scraper.py --inspect   # print the hero column with slot numbers, store nothing

Env: SLOTS (default "10,11,12"), COLUMN (default "col_2"), FEED_URL
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


def load_env(path=Path(__file__).with_name(".env")):
    """Minimal .env loader (no extra dependency). Real env vars win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

FEED_URL = os.getenv("FEED_URL",
                     "https://timesofindia.indiatimes.com/webhomepagefeedspadata.cms?feedtype=sjson")
SLOTS = [int(s) for s in os.getenv("SLOTS", "10,11,12").split(",")]
COLUMN = os.getenv("COLUMN", "col_2")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
LOCAL_DIR = Path(os.getenv("LOCAL_DIR", "hero_data"))

if SUPABASE_URL and (not SUPABASE_KEY or "paste-your" in SUPABASE_KEY):
    raise SystemExit("SUPABASE_KEY missing in .env — paste the service_role key "
                     "(Supabase -> Project Settings -> API Keys).")


# ---------------- feed ----------------

def image_url(item):
    iid = item.get("imageid")
    if not iid:
        return ""
    size = f",imgsize-{item['imagesize']}" if item.get("imagesize") else ""
    return (f"https://static.toiimg.com/thumb/msid-{iid},width-400,height-225"
            f"{size},resizemode-75/photo.jpg")


def fetch_feed():
    r = requests.get(FEED_URL, timeout=30, headers={
        "User-Agent": "TOI-hero-slot-tracker/1.0", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def hero_cards(feed):
    """Hero column as rendered: list of cards in on-screen order, slot = 1-based."""
    sec = next((s for s in feed.get("sections", []) if s.get("id") == "topstories"), None)
    if not sec:
        raise SystemExit("No 'topstories' section in feed — structure changed?")
    col = next((c for c in sec.get("items", []) if c.get("tn") == COLUMN), None)
    if not col:
        raise SystemExit(f"Column {COLUMN} not found in topstories")
    # Pinned location: slot N == col_2.items[N-1], whatever the CMS put there.
    cards = []
    for idx, it in enumerate(col.get("items", [])):
        tn = it.get("tn", "")
        base = {"slot": idx + 1, "api_index": idx,
                "api_path": f'sections[id=topstories].items[tn={COLUMN}].items[{idx}]'}
        if tn == "ad":
            unit = it.get("adCode", "").rsplit("/", 1)[-1]
            cards.append({**base, "kind": "ad", "key": f"ad:{unit}", "gaindex": None,
                          "headline": f"[AD] {unit}", "url": "", "msid": "",
                          "item_type": "ad", "image_id": "", "image_url": ""})
        elif it.get("hl"):
            gi = it.get("gaindex")
            cards.append({**base, "kind": "story", "key": it.get("id") or it.get("wu", ""),
                          "gaindex": int(gi) if gi else None,
                          "headline": it["hl"].strip(), "url": it.get("wu", ""),
                          "msid": it.get("id", ""), "item_type": tn,
                          "image_id": it.get("imageid", ""), "image_url": image_url(it)})
        else:  # widget / audio player / anything without a headline
            label = (it.get("name") or tn or "unknown").strip()
            cards.append({**base, "kind": "widget", "key": f"widget:{tn}", "gaindex": None,
                          "headline": f"[WIDGET] {label}", "url": "", "msid": "",
                          "item_type": tn, "image_id": "", "image_url": ""})
    return cards


# ---------------- storage ----------------

def sb(method, table, **kw):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json", **kw.pop("headers", {})}
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{table}", headers=h, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.text else None


def load_last_run():
    """-> (prev_cards list or None, {slot: prev slot row})"""
    if SUPABASE_URL:
        runs = sb("GET", "hero_runs", params={"order": "run_at.desc", "limit": "1",
                                               "select": "id,cards"})
        if not runs:
            return None, {}
        rows = sb("GET", "hero_slots", params={"run_id": f"eq.{runs[0]['id']}"})
        return runs[0]["cards"], {r["slot"]: r for r in rows}
    f = LOCAL_DIR / "hero_runs.jsonl"
    if not f.exists():
        return None, {}
    for line in reversed(f.read_text(encoding="utf-8").strip().splitlines()):
        last = json.loads(line)
        if "cards" in last and "slot_rows" in last:   # skip rows from older versions
            return last["cards"], {int(r["slot"]): r for r in last["slot_rows"]}
    return None, {}


def save(run_at, cards, slot_rows, events):
    if SUPABASE_URL:
        run = sb("POST", "hero_runs", headers={"Prefer": "return=representation"},
                 json={"run_at": run_at, "cards": cards})
        rid = run[0]["id"]
        for r in slot_rows + events:
            r["run_id"] = rid
        sb("POST", "hero_slots", json=slot_rows)
        if events:
            sb("POST", "hero_changes", json=events)
        return
    LOCAL_DIR.mkdir(exist_ok=True)
    with (LOCAL_DIR / "hero_runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_at": run_at, "cards": cards, "slot_rows": slot_rows},
                            ensure_ascii=False) + "\n")
    for name, rows in (("hero_slots.csv", slot_rows), ("hero_changes.csv", events)):
        if not rows:
            continue
        f = LOCAL_DIR / name
        new = not f.exists()
        with f.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if new:
                w.writeheader()
            w.writerows(rows)


# ---------------- diff ----------------

def minutes_between(a, b):
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60, 1)


def main():
    cards = hero_cards(fetch_feed())

    if "--inspect" in sys.argv:
        for c in cards:
            mark = "  <== tracked" if c["slot"] in SLOTS else ""
            gi = f"gaindex {c['gaindex']}" if c["gaindex"] else "        "
            print(f"slot {c['slot']:>2} ({gi:>10}) [{c['item_type']:<8}] {c['headline'][:80]}{mark}")
        return

    run_at = datetime.now(timezone.utc).isoformat()
    prev_cards, prev_slots = load_last_run()
    now_pos = {c["key"]: c["slot"] for c in cards}
    before_pos = {c["key"]: c["slot"] for c in (prev_cards or [])}
    before_hl = {c["key"]: c["headline"] for c in (prev_cards or [])}
    by_slot = {c["slot"]: c for c in cards}

    slot_rows, events, log = [], [], []
    for slot in SLOTS:
        cur, old = by_slot.get(slot), prev_slots.get(slot)
        if not cur:
            log.append(f"slot {slot}: not present in feed")
            continue
        story_changed = bool(old) and old["key"] != cur["key"]
        headline_changed = bool(old) and not story_changed and old["headline"] != cur["headline"]
        image_changed = (bool(old) and not story_changed
                         and str(old.get("image_id") or "") != str(cur["image_id"]))
        since = run_at if (not old or story_changed) else old["occupant_since"]

        slot_rows.append({
            "run_at": run_at, "slot": slot, "api_path": cur["api_path"],
            "kind": cur["kind"], "key": cur["key"],
            "gaindex": cur["gaindex"], "headline": cur["headline"], "url": cur["url"],
            "msid": cur["msid"], "item_type": cur["item_type"],
            "image_id": cur["image_id"], "image_url": cur["image_url"],
            "occupant_since": since,
            "story_changed": story_changed, "headline_changed": headline_changed,
            "image_changed": image_changed,
        })

        change = ("story" if story_changed else "headline" if headline_changed
                  else "image" if image_changed else None)
        if change:
            came_from = before_pos.get(cur["key"])
            went_to = now_pos.get(old["key"])
            events.append({
                "run_at": run_at, "slot": slot, "change_type": change,
                "old_headline": old["headline"], "old_url": old["url"],
                "old_image_url": old["image_url"], "old_kind": old["kind"],
                "new_headline": cur["headline"], "new_url": cur["url"],
                "new_image_url": cur["image_url"], "new_kind": cur["kind"],
                # story swaps only: movement around the hero column
                "new_came_from_slot": came_from if change == "story" else None,
                "new_is_fresh": (change == "story" and came_from is None),
                # incoming story was already on the hero but its headline got rewritten
                "new_headline_edited": (change == "story" and came_from is not None
                                        and before_hl.get(cur["key"]) != cur["headline"]),
                "new_prev_headline": before_hl.get(cur["key"]) if change == "story" else None,
                "old_went_to_slot": went_to if change == "story" else None,
                "old_dropped_out": (change == "story" and went_to is None),
                "old_held_minutes": minutes_between(old["occupant_since"], run_at)
                                    if change == "story" else None,
            })
            if change == "story":
                src = f"from slot {came_from}" if came_from else "new to hero"
                if came_from and before_hl.get(cur["key"]) != cur["headline"]:
                    src += ", headline rewritten"
                dst = f"moved to slot {went_to}" if went_to else "dropped out"
                log.append(f"slot {slot} STORY: '{old['headline'][:45]}' ({dst}) -> "
                           f"'{cur['headline'][:45]}' ({src})")
            else:
                log.append(f"slot {slot} {change.upper()} edit: '{old['headline'][:45]}' -> "
                           f"'{cur['headline'][:45]}'")
        else:
            log.append(f"slot {slot} {'first run' if not old else 'no change'}: "
                       f"{cur['headline'][:75]}")

    if not slot_rows:
        raise SystemExit("No tracked slots found:\n" + "\n".join(log))
    save(run_at, cards, slot_rows, events)
    print(run_at)
    print("\n".join(log))


if __name__ == "__main__":
    main()