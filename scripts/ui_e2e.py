"""Browser test for the web client: real Chrome, real clicks, real server.

    python scripts/ui_e2e.py [--api http://localhost:8080] [--shots DIR] [--headed]

The web client broke twice in a row (INC-007, INC-008) for reasons no Python
test could see, because nothing loaded the page in a browser. This does.

Per scenario it opens the page, lets the deep link run the claim, waits for
the verdict, and asserts:

  * the recommendation says what the scenario is meant to produce
  * the pipeline reached the right end state (done / human / stopped)
  * no JavaScript error was raised
  * every citation card is a resolving quote with a role badge
  * the policy evidence is COLLAPSED by default, and the decision buttons are
    on screen without scrolling

then it drives the workspace the way an officer would, in a clean browser:

  * the Claims drawer opens, filters, searches, and closes (button, scrim, Esc)
  * "Needs your attention" starts empty and honest, fills from real triage
    results, lists only claims that need a person, most pressing first
  * opening a claim from it, expanding the evidence and accepting the
    recommendation all keep the same claim selected, and the claim then
    leaves the attention list

and saves a full-page screenshot of each state, so a person can look.

Uses the Chrome or Edge already installed (Playwright's `channel`), so it needs
`pip install playwright` but no browser download.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

#: scenario id -> (stamp must contain one of, caption class)
EXPECT = {
    "food":      (["not covered"], "done"),
    "commute":   (["covered"], "done"),
    "ordinary":  (["covered"], "done"),
    "rideshare": (["not covered"], "done"),
    "injury":    (["covered", "not covered", "partly", "can't tell"], "done"),
    "theft":     (["covered", "not covered", "partly", "can't tell"], "done"),
    "injection": (["blocked"], "stop"),
    "invalid":   (["rejected"], "stop"),
    "distress":  (["covered", "not covered", "partly", "can't tell"], "done"),
    "cyber":     (["covered", "not covered", "partly", "can't tell", "hand to a human"], None),
}
#: scenarios whose result carries citations and so an evidence section
HAS_EVIDENCE = {"food", "commute", "ordinary", "rideshare", "injury", "theft", "distress", "cyber"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--shots", default=None, help="directory for screenshots")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--only", default=None, help="a scenario id, or: outage, drawer, queue")
    parser.add_argument("--theme", default=None, choices=[None, "light", "dark"])
    parser.add_argument("--width", type=int, default=1440)
    args = parser.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    height = 1000

    def wanted(name: str) -> bool:
        return not args.only or args.only == name

    failures: list[str] = []

    def shot(page, name: str) -> None:
        if shots:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=True)

    with sync_playwright() as pw:
        browser = None
        for channel in ("chrome", "msedge"):
            try:
                browser = pw.chromium.launch(channel=channel, headless=not args.headed)
                break
            except Exception:
                continue
        if browser is None:
            print("no Chrome or Edge found for Playwright to drive")
            return 2

        def new_context():
            ctx = browser.new_context(viewport={"width": args.width, "height": height},
                                      color_scheme=args.theme or "light")
            if args.theme:
                ctx.add_init_script(f"try{{localStorage.setItem('theme','{args.theme}')}}catch(e){{}}")
            return ctx

        context = new_context()

        # ---- the empty page first: does it load without errors? -------------
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(args.api + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#claimsbtn", timeout=15000)
        page.wait_for_function("() => document.getElementById('b-model').innerText.length > 0 && !/checking|connecting/.test(document.getElementById('b-model').innerText)", timeout=20000)
        n = page.locator("#claims-count").inner_text()
        print(f"  page loaded: {n} claims, model badge: {page.locator('#b-model').inner_text()!r}")
        if n != "10":
            failures.append(f"the Claims button should count the 10 sample claims, shows {n!r}")
        if page.locator("#verdict .stamp").count():
            failures.append("a fresh page shows a recommendation before any claim was analysed")
        if "Run analysis" not in page.locator("#verdict").inner_text():
            failures.append("an unanalysed claim does not prompt the officer to run the analysis")
        if errors:
            failures.append(f"empty page raised JS errors: {errors}")
        shot(page, "00-empty")
        page.close()

        # ---- the drawer ------------------------------------------------------
        if wanted("drawer"):
            page = context.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            page.goto(args.api + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#claimsbtn", timeout=15000)
            ok = True
            if page.locator("#drawer").is_visible():
                failures.append("drawer: the claims drawer is visible before anyone asked for it")
                ok = False
            page.click("#claimsbtn")
            page.wait_for_selector("#drawer.open .cl", timeout=5000)
            page.wait_for_timeout(350)
            if page.locator("#dlist .cl").count() != 10:
                failures.append(f"drawer: expected 10 claims listed, found {page.locator('#dlist .cl').count()}")
                ok = False
            tabs = [t.strip().lower() for t in page.locator("#tabs .tab").all_inner_texts()]
            for want in ("all claims", "pending review", "under investigation", "completed", "escalated"):
                if not any(want in t for t in tabs):
                    failures.append(f"drawer: missing the {want!r} filter ({tabs})")
                    ok = False
            shot(page, "drawer")
            page.fill("#claim-search", "delivery")
            page.wait_for_timeout(150)
            hits = page.locator("#dlist .cl").count()
            if hits != 1:
                failures.append(f"drawer: searching 'delivery' should find 1 claim, found {hits}")
                ok = False
            page.fill("#claim-search", "zzz-no-such-claim")
            page.wait_for_timeout(150)
            if "No claims match" not in page.locator("#dlist").inner_text():
                failures.append("drawer: an empty search result shows no explanation")
                ok = False
            page.fill("#claim-search", "")
            page.click('[data-tab="escalated"]')
            page.wait_for_timeout(100)
            if page.locator("#dlist .cl").count() != 0:
                failures.append("drawer: nothing has been analysed, so 'Escalated' should be empty")
                ok = False
            page.click('[data-tab="all"]')
            # choosing a claim loads it and closes the drawer
            page.click('#dlist [data-claim="commute"]')
            page.wait_for_timeout(350)
            if page.locator("#drawer").is_visible():
                failures.append("drawer: choosing a claim should close the drawer")
                ok = False
            ref = page.locator("#w-id").inner_text()
            if "Not analysed" not in page.locator("#w-status").inner_text():
                failures.append("drawer: a chosen, unanalysed claim should say so in its header")
                ok = False
            # three ways to close it
            for how in ("scrim", "escape", "button"):
                page.click("#claimsbtn")
                page.wait_for_selector("#drawer.open", timeout=3000)
                page.wait_for_timeout(300)
                if how == "scrim":
                    page.mouse.click(args.width - 20, 400)
                elif how == "escape":
                    page.keyboard.press("Escape")
                else:
                    page.click("#drawer-close")
                page.wait_for_timeout(350)
                if page.locator("#drawer").is_visible():
                    failures.append(f"drawer: did not close with {how}")
                    ok = False
            if page.locator("#w-id").inner_text() != ref:
                failures.append("drawer: opening and closing it changed the selected claim")
                ok = False
            if errs:
                failures.append(f"drawer: JS errors {errs}")
                ok = False
            print(f"  {'PASS' if ok else 'FAIL'}  drawer     opens, searches, filters, selects, closes three ways")
            page.close()

        # ---- every scenario, by deep link -----------------------------------
        for sid, (want, cap) in EXPECT.items():
            if not wanted(sid):
                continue
            page = context.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            started = time.time()
            # a fresh URL each time so the deep link fires on a clean load
            page.goto(f"{args.api}/?s={sid}-{int(started)}#run={sid}", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#verdict .stamp", timeout=90000)
                page.wait_for_function("() => /caption (done|stop|human)/.test(document.getElementById('caption').className)", timeout=90000)
                page.wait_for_timeout(900)  # let the stamp and cards finish animating
            except Exception as exc:
                failures.append(f"{sid}: no verdict appeared ({exc.__class__.__name__})")
                shot(page, f"FAIL-{sid}")
                page.close()
                continue

            stamp = page.locator("#verdict .stamp").inner_text().lower().replace("\n", " ")
            capcls = page.get_attribute("#caption", "class") or ""
            cites = page.locator(".clause").count()
            roles = page.locator(".clause .role").count()
            ok = any(w in stamp for w in want)
            if not ok:
                failures.append(f"{sid}: stamp {stamp!r} not in {want}")
            if cap and cap not in capcls and not ("human" in capcls):
                failures.append(f"{sid}: caption ended as {capcls!r}, expected {cap!r}")
            if cites != roles:
                failures.append(f"{sid}: {cites} citation cards but {roles} role badges")
            if sid in HAS_EVIDENCE:
                if page.locator("#evid").count() == 0:
                    failures.append(f"{sid}: no evidence section")
                    ok = False
                else:
                    if page.eval_on_selector("#evid", "e => e.open"):
                        failures.append(f"{sid}: policy evidence should be collapsed by default")
                        ok = False
                    if page.locator("#evid .clause").first.is_visible():
                        failures.append(f"{sid}: policy clauses are visible while the evidence is collapsed")
                        ok = False
                if args.width > 920:  # side by side: the dock is on screen at once
                    box = page.locator('[data-fb="satisfactory"]').bounding_box()
                    if not box or box["y"] + box["height"] > height:
                        failures.append(f"{sid}: the Accept button is not on screen without scrolling (box={box})")
                        ok = False
                # the page must never scroll sideways, at any width
                wide = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if wide > 1:
                    failures.append(f"{sid}: the page overflows the window by {wide}px horizontally")
                    ok = False
                # and nothing in the evaluation panel may spill past its card
                spill = page.evaluate("() => { const c = document.getElementById('verdict').getBoundingClientRect(); "
                                      "return [...document.querySelectorAll('#verdict *')].filter(e => e.getBoundingClientRect().width && "
                                      "e.getBoundingClientRect().right > c.right + 1).length }")
                if spill:
                    failures.append(f"{sid}: {spill} element(s) in the evaluation panel overflow its card")
                    ok = False
                if sid == "food" and "reward" not in page.locator(".kq").inner_text().lower():
                    failures.append("food: the key question about reward or favour is not shown")
                    ok = False
            if errs:
                failures.append(f"{sid}: JS errors {errs}")
            print(f"  {'PASS' if ok and not errs else 'FAIL'}  {sid:<10} stamp={stamp!r:<40} citations={cites}  "
                  f"{time.time() - started:4.1f}s")
            shot(page, sid)
            if sid == "food":
                # opening the log and the technical trace must not widen the page or slide
                # the workspace under the evaluation panel (the trace holds a wide table and JSON)
                for sel in ("#logbox", "#trace"):
                    page.click(f"{sel} > summary")
                    page.wait_for_timeout(250)
                    if not page.eval_on_selector(sel, "e => e.open"):
                        failures.append(f"food: {sel} did not open")
                wide = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if wide > 1:
                    failures.append(f"food: opening the trace widens the page by {wide}px")
                clash = page.evaluate("() => { const a = document.getElementById('activity').getBoundingClientRect(); "
                                      "const v = document.getElementById('verdict').getBoundingClientRect(); "
                                      "return window.innerWidth > 920 ? a.right - v.left : -1 }")
                if clash > 1:
                    failures.append(f"food: with the trace open the activity card overlaps the evaluation panel by {clash}px")
                shot(page, "food-trace-open")
                for sel in ("#trace", "#logbox"):
                    page.click(f"{sel} > summary")
                # opening the evidence must not reset the selected claim
                ref = page.locator("#w-id").inner_text()
                page.click("#evid summary")
                page.wait_for_timeout(400)
                if not page.locator("#evid .clause").first.is_visible():
                    failures.append("food: expanding the evidence did not show the clauses")
                if page.locator("#evid .clause").count() < 3:
                    failures.append("food: the evidence should hold the grant, the exclusion and the restriction")
                if "but" not in page.locator("#evid").inner_text().lower():
                    failures.append("food: the grant-to-exclusion relationship ('but') is not shown")
                if page.locator("#w-id").inner_text() != ref:
                    failures.append("food: expanding the evidence changed the selected claim")
                # stacked layout: bring the panel into view first; the dock must then stay on screen
                page.locator("#verdict").scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                box = page.locator('[data-fb="satisfactory"]').bounding_box()
                if not box or box["y"] + box["height"] > height:
                    failures.append(f"food: with the evidence open the Accept button scrolled out of view (box={box})")
                spill = page.evaluate("() => { const c = document.getElementById('verdict').getBoundingClientRect(); "
                                      "return [...document.querySelectorAll('#verdict *')].filter(e => e.getBoundingClientRect().width && "
                                      "e.getBoundingClientRect().right > c.right + 1).length }")
                if spill:
                    failures.append(f"food: {spill} element(s) overflow the evaluation card with the evidence open")
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(150)
                shot(page, "food-evidence-open")
            page.close()

        # ---- the outage switch: demonstration item 8, in the browser ---------
        if wanted("outage"):
            page = context.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            page.goto(args.api + "/?outage", wait_until="domcontentloaded")
            page.wait_for_selector("#claimsbtn", timeout=15000)
            page.wait_for_timeout(800)
            if page.is_disabled("#outage"):
                print("  SKIP  outage     (queue mode: the breaker lives in the worker)")
            else:
                page.click("#outage")
                page.wait_for_timeout(400)
                page.check("#nocache")  # a cached answer would make no model call to fail
                page.click("#claimsbtn")
                page.wait_for_selector("#drawer.open", timeout=3000)
                page.wait_for_timeout(300)
                page.click('#dlist [data-claim="ordinary"]')
                page.wait_for_timeout(300)
                page.check("#nocache")
                page.click("#go")
                try:
                    page.wait_for_function(
                        "() => /human/i.test((document.querySelector('#verdict .stamp') || {}).innerText || '')",
                        timeout=60000)
                    page.wait_for_timeout(900)
                    stamp = page.locator("#verdict .stamp").inner_text().lower().replace("\n", " ")
                    ok = "human" in stamp
                    if not ok:
                        failures.append(f"outage: expected a hand-off to a human, got {stamp!r}")
                    print(f"  {'PASS' if ok else 'FAIL'}  outage     stamp={stamp!r}")
                    shot(page, "outage")
                except Exception as exc:
                    failures.append(f"outage: no hand-off to a human ({exc.__class__.__name__})")
                page.click("#outage")  # restore the provider for anything that runs next
            if errs:
                failures.append(f"outage: JS errors {errs}")
            page.close()

        # ---- the attention queue, in a clean browser -------------------------
        if wanted("queue"):
            ctx = new_context()
            page = ctx.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            page.goto(args.api + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#attn .calm", timeout=15000)
            ok = True
            if "Nothing triaged yet" not in page.locator("#attn").inner_text():
                failures.append("queue: with nothing analysed the section should say so, not claim 'all caught up'")
                ok = False
            if page.locator("#attn .att").count():
                failures.append("queue: attention cards appeared before any claim was analysed")
                ok = False
            shot(page, "queue-empty")
            page.click("[data-triage]")
            # the batch is over when every claim has a result or a gate decision
            page.wait_for_function("() => !state.batch && claims.every(c => c.run || c.gate)", timeout=180000)
            page.wait_for_timeout(700)
            cards = page.locator("#attn .att")
            n_cards = cards.count()
            texts = [cards.nth(i).inner_text().lower() for i in range(n_cards)]
            refs = [cards.nth(i).locator(".ref").inner_text() for i in range(n_cards)]
            if not 1 <= n_cards <= 4:
                failures.append(f"queue: expected 1-4 attention cards, found {n_cards}")
                ok = False
            if n_cards and "distress" not in texts[0]:
                failures.append(f"queue: the urgent welfare claim should be first, got: {texts[0][:90]!r}")
                ok = False
            ordinary_ref = page.evaluate("() => claims.find(c => c.id === 'ordinary').ref")
            if ordinary_ref in refs:
                failures.append("queue: a normal-priority claim well inside its SLA is listed as needing attention")
                ok = False
            for t in texts:
                if not any(w in t for w in ("urgent", "high priority", "overdue", "deadline", "person", "distress")):
                    failures.append(f"queue: a card gives no reason it needs attention: {t[:90]!r}")
                    ok = False
                if "review claim" not in t:
                    failures.append(f"queue: a card has no clear action: {t[:90]!r}")
                    ok = False
            shot(page, "queue")

            # open the most pressing claim from its card
            first_ref = refs[0] if refs else ""
            cards.first.click()
            page.wait_for_selector("#verdict .stamp", timeout=10000)
            page.wait_for_timeout(500)
            if page.locator("#w-id").inner_text() != first_ref:
                failures.append("queue: clicking a card did not open that claim in the workspace")
                ok = False
            if page.locator("#evid").count() == 0 or page.eval_on_selector("#evid", "e => e.open"):
                failures.append("queue: a claim opened from a card should have its evidence collapsed")
                ok = False
            page.click("#evid summary")
            page.wait_for_timeout(300)
            if page.locator("#w-id").inner_text() != first_ref:
                failures.append("queue: expanding evidence changed the selected claim")
                ok = False
            box = page.locator('[data-fb="satisfactory"]').bounding_box()
            if not box or box["y"] + box["height"] > height:
                failures.append(f"queue: the Accept button is not visible with the evidence open (box={box})")
                ok = False

            # accept it: recorded, the claim leaves the attention list
            page.click('[data-fb="satisfactory"]')
            page.wait_for_function("() => /recorded/i.test(document.getElementById('fbmsg').innerText)", timeout=10000)
            page.wait_for_timeout(300)
            if first_ref in page.locator("#attn").inner_text():
                failures.append("queue: a claim the officer has decided is still listed as needing attention")
                ok = False
            if "Awaiting your decision" in page.locator("#w-status").inner_text():
                failures.append("queue: the claim header still says it awaits a decision")
                ok = False
            shot(page, "queue-after-accept")

            # a reload keeps the queue (results are re-fetched by run id)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelectorAll('#attn .att').length > 0 || /caught up/i.test(document.getElementById('attn').innerText)", timeout=15000)
            page.wait_for_timeout(600)
            page.click("#claimsbtn")
            page.wait_for_selector("#drawer.open", timeout=3000)
            page.wait_for_timeout(300)
            analysed = page.locator("#dsub").inner_text()
            if not analysed.startswith(("8 of 10", "9 of 10", "10 of 10", "7 of 10")):
                failures.append(f"queue: after a reload the analysed claims were lost ({analysed!r})")
                ok = False
            done = page.locator('[data-tab="completed"] .n').inner_text()
            inv = page.locator('[data-tab="investigating"] .n').inner_text()
            if int(done) + int(inv) < 1:
                failures.append("queue: an accepted claim should count under Completed or Under investigation")
                ok = False
            shot(page, "drawer-after-triage")
            if errs:
                failures.append(f"queue: JS errors {errs}")
                ok = False
            print(f"  {'PASS' if ok else 'FAIL'}  queue      {n_cards} attention cards, most pressing first; accept clears it; reload keeps it")
            page.close()
            ctx.close()
        browser.close()

    print()
    if failures:
        print(f"{len(failures)} UI failure(s):")
        for f in failures:
            print("  -", f)
        return 1
    print("web client: every scenario produced the expected verdict, with no script errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
