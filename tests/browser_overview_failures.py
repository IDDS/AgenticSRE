"""Run on a remote QA host: python tests/browser_overview_failures.py <dashboard-url>."""
import asyncio
from collections import Counter
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright


def snapshot(namespaces):
    node = {"name": "node-a", "roles": [], "cpu": "8", "memory": "4Gi", "ready": False, "version": "v1.31"}
    pods = [
        {"name": "pod-" + ns, "node": "node-a", "namespace": ns, "phase": "Pending", "ready": "0/1",
         "restarts": 0, "labels": {}, "is_faulty": True, "fault_reason": "Pending"}
        for ns in namespaces
    ]
    return {"nodes": [node] if pods else [], "pods": pods, "deployments": [], "services": [],
            "namespaces": namespaces, "summary": {"total_nodes": int(bool(pods)), "total_pods": len(pods),
            "total_deployments": 0, "total_services": 0, "faulty_pods": len(pods), "faulty_deployments": 0}}


async def main(url):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()
        page.set_default_timeout(12000)
        counts = Counter()
        errors = []
        data = snapshot(["blue", "green"])
        warning = {"type": "Warning", "reason": "BackOff", "object": "pod-one", "message": "retrying"}
        mode = "ok"
        gate = asyncio.Event()
        held = asyncio.Event()
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def serve(route):
            path = urlparse(route.request.url).path.rsplit("/", 1)[-1]
            counts[path] += 1
            if (mode == "held" and path == "events") or (mode == "timeout" and path == "topology"):
                held.set()
                await gate.wait()
                if mode == "timeout":
                    await route.abort("timedout")
                    return
            if mode == "unavailable":
                await route.fulfill(status=503, json={"detail": "unavailable"})
            elif mode == "invalid" and path == "topology":
                await route.fulfill(status=200, body="<html>proxy response</html>")
            elif path == "events":
                await route.fulfill(json={"events": [warning] if data["pods"] else []})
            elif path == "topology":
                await route.fulfill(json=data)
            elif path == "overview":
                await route.fulfill(json={"nodes": 0, "pods_total": 0, "namespaces": 0, "pod_phases": {}})
            elif path == "nodes":
                await route.fulfill(json={"nodes": []})
            else:
                await route.abort()

        await page.route("**/api/cluster/**", serve)
        await page.goto(url, wait_until="domcontentloaded")
        await page.select_option("#refresh-interval", "0")
        await page.wait_for_function("state.podChart && document.querySelector('#alert-preview').textContent.includes('BackOff')")
        assert await page.locator("#node-grid .badge-danger").text_content() == "NotReady"
        await page.select_option("#topo-ns-filter", "green")
        await page.locator('[data-topo-view="dependency"]').click()
        assert await page.locator("#topo-dependency-map").evaluate("el => el.classList.contains('active')")
        data = snapshot(["blue"])
        await page.evaluate("loadOverview()")
        assert await page.evaluate("_topo.nsFilter === '' && document.querySelector('#topo-ns-filter').value === ''")
        assert await page.locator("#topo-node-map .pod-dot").count() == 1

        mode = "held"
        before = counts.copy()
        await page.evaluate("() => { for (let i=0;i<8;i++) document.querySelector('button[onclick=\"loadOverview()\"]').click(); }")
        await asyncio.wait_for(held.wait(), 10)
        await page.wait_for_function("overviewSnapshotRequest === null && overviewEventsRequest !== null")
        assert counts - before == {"topology": 1, "events": 1}
        await page.evaluate("switchView('workswarm')")
        assert await page.evaluate("state.currentView === 'workswarm'")
        gate.set()
        await page.wait_for_function("overviewEventsRequest === null")
        assert await page.evaluate("state.currentView === 'workswarm'")
        mode = "ok"
        await page.evaluate("switchView('overview')")
        await page.wait_for_function("overviewSnapshotRequest === null && overviewEventsRequest === null")

        mode = "unavailable"
        await page.evaluate("loadOverview()")
        assert "更新失败" in await page.locator("#overview-snapshot-status").text_content()
        assert "更新失败" in await page.locator("#overview-events-status").text_content()
        assert "BackOff" in await page.locator("#alert-preview").text_content()
        assert await page.locator("#topo-node-map .pod-dot").count() == 1
        mode = "invalid"
        await page.evaluate("loadOverview()")
        assert await page.locator("#node-grid .node-card").count() == 0
        assert await page.locator("#overview-events-status").text_content() == ""

        mode = "timeout"
        gate, held = asyncio.Event(), asyncio.Event()
        before = counts.copy()
        await page.evaluate("() => { window.timeoutRefresh = loadOverview(); }")
        await asyncio.wait_for(held.wait(), 10)
        await page.wait_for_function("overviewSnapshotRequest === null", timeout=42000)
        assert counts - before == {"topology": 1, "events": 1, "overview": 1, "nodes": 1}
        assert "更新失败" in await page.locator("#overview-snapshot-status").text_content()
        gate.set()
        await asyncio.sleep(0.1)
        mode = "ok"
        data = snapshot([])
        await page.evaluate("loadOverview()")
        assert await page.locator("#overview-snapshot-status").text_content() == ""
        assert "暂无告警" in await page.locator("#alert-preview").text_content()
        await page.wait_for_function("document.querySelector('#stat-pods').textContent === '0'")

        before = counts.copy()
        await page.select_option("#refresh-interval", "10")
        await asyncio.sleep(10.8)
        await page.select_option("#refresh-interval", "0")
        assert counts - before == {"topology": 1, "events": 1}
        # An older number animation must not overwrite the newer empty snapshot.
        data = snapshot([f"ns-{index}" for index in range(25)])
        await page.evaluate("loadOverview()")
        data = snapshot([])
        await page.evaluate("loadOverview()")
        await asyncio.sleep(0.7)
        assert await page.locator("#stat-pods").text_content() == "0"
        before = counts.copy()
        await page.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")
        await page.select_option("#refresh-interval", "10")
        await asyncio.sleep(10.8)
        await page.select_option("#refresh-interval", "0")
        await page.evaluate("delete document.hidden")
        assert counts == before
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.evaluate("loadOverview()")
        assert not errors, errors
        print("Browser checks passed: namespace removal, NotReady, duplicate clicks, slow events, view switch, "
              "503, malformed response, 30s timeout, empty cluster, recovery, automatic refresh, mobile viewport")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
