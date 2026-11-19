"""Read a saved investigation through Chromium and verify its hypothesis citations."""
import argparse
import json
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright


def verify(page, base_url, run_id, screenshots):
    api = f"{base_url}/api/rca/{quote(run_id, safe='')}"
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_function("typeof ontologyExplorer !== 'undefined'")
    page.evaluate("id => ontologyExplorer.fromRun(id)", run_id)
    panel = page.locator("#og-hypotheses")
    expect(panel).to_be_visible()
    with page.expect_response(lambda r: r.url.startswith(api + "/strategy?")) as response:
        panel.locator(":scope > summary").click()
    assert response.value.ok
    strategy = response.value.json()
    revision = strategy["ontology_revision"]
    assert strategy["run_id"] == run_id
    expect(page.locator("#og-scope")).to_contain_text(revision)
    expected = set()
    for stage, rows in [(f"第 {r['iteration']} 轮" if r.get("iteration") is not None else "轮次未记录",
                         r.get("after", [])) for r in strategy["rounds"]] + [
            (f"独立角色复核 {i}", r.get("items", [])) for i, r in enumerate(strategy["independent_reviews"], 1)]:
        for row in rows:
            for field, role in (("supporting_evidence_ids", "支持"), ("contradicting_evidence_ids", "反驳")):
                expected.update((row["id"], stage, role, ref) for ref in row.get(field, []))
    assert expected, "This investigation has no recorded round or review citations"
    displayed, checked, captured = set(), {}, set()
    bound_target = None
    groups = panel.locator(":scope > div > details")
    expect(groups.first).to_be_visible()
    for group in groups.all():
        summary = group.locator(":scope > summary")
        candidate = summary.inner_text().split(" · ", 1)[0]
        summary.click()
        expect(group.locator(".og-hyp-stage").first).to_be_visible()
        for section in group.locator(".og-hyp-stage").all():
            stage = section.locator("h4").inner_text().split(" · ", 1)[0]
            menu = section.locator("select")
            if not menu.count():
                continue
            for index, option in enumerate(menu.locator("option").all()[1:], 1):
                ref = option.get_attribute("value")
                role = option.inner_text().split(" · ", 1)[0]
                displayed.add((candidate, stage, role, ref))
                if (ref, role) in checked:
                    continue
                rows = page.request.get(api + "/ontology/observations", params={
                    "revision": revision, "record_id": ref, "limit": 1}).json()["items"]
                menu.select_option(index=index)
                if rows:
                    row, record = rows[0], section.locator(".og-record")
                    assert row["id"] == ref
                    expect(record.locator("small").first).to_contain_text(ref)
                    expect(record.locator("p").first).to_have_text(row["observation"])
                    locate = section.get_by_role("button", name="在图谱中定位观测对象")
                    assert locate.count() == int(bool(row["entity_id"]))
                    if row["entity_id"] and bound_target is None:
                        bound_target = (summary, menu, index, section, ref, row["entity_id"])
                    if screenshots and role in ("支持", "反驳") and role not in captured:
                        screenshots.mkdir(parents=True, exist_ok=True)
                        section.screenshot(path=str(screenshots / ("support.png" if role == "支持" else "counter.png")))
                        captured.add(role)
                else:
                    expect(section).to_contain_text("未匹配到本次调查版本中的原始观测")
                checked[ref, role] = bool(rows)
        summary.click()
    assert expected <= displayed, f"{len(expected - displayed)} recorded citation roles missing"
    candidates = groups.count()
    if bound_target:
        summary, menu, index, section, ref, entity_id = bound_target
        summary.click()
        menu.select_option(index=index)
        expect(section.locator(".og-record small").first).to_contain_text(ref)
        with page.expect_response(lambda r: r.url.startswith(api + "/ontology-graph?")) as response:
            section.get_by_role("button", name="在图谱中定位观测对象").click()
        graph = response.value.json()
        assert graph["focus"]["id"] == entity_id and graph["revision"] == revision
        expect(page.locator(".og-center h3")).to_have_text(graph["focus"]["name"])
        if graph["focus"]["uid"]:
            expect(page.locator(".og-center code")).to_have_text(graph["focus"]["uid"])
    return {"run": run_id, "candidates": candidates, "rounds": len(strategy["rounds"]),
            "reviews": len(strategy["independent_reviews"]), "recorded_round_review_citations": len(expected),
            "unique_references": len({ref for ref, _ in checked}), "bound_object_opened": bool(bound_target),
            "resolved_reference_roles": sum(checked.values()), "unresolved_reference_roles": sum(not v for v in checked.values())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--chromium")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=args.chromium)
        try:
            print(json.dumps(verify(browser.new_page(), args.base_url.rstrip("/"), args.run_id, args.screenshots)))
        finally:
            browser.close()
