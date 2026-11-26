"""Run in a remote test environment with Playwright, kubectl and the same cluster as the dashboard."""
import argparse
import asyncio
import json
import subprocess
import uuid

from playwright.async_api import async_playwright


async def open_rca(page):
    item = page.locator('[data-view="rca"]').first
    if not await item.is_visible():
        await page.locator('summary').filter(has_text='高级工具').click()
    await item.click()


def verify_evidence(evidence, namespace, service):
    def read(*args):
        command = ['kubectl', '-n', namespace, 'get', *args, '-o', 'json']
        return json.loads(subprocess.check_output(command, timeout=30))

    selector = read('service', service)['spec']['selector']
    assert selector, "验收 Service 需要 Pod 选择器"
    observations = {row['tool']: row['observation'] for row in evidence if row['status'] == 'ok'}
    assert observations['read_service']['selector'] == selector
    pods = read('pods', '-l', ','.join(f'{k}={v}' for k, v in sorted(selector.items())))
    assert observations['read_pods']['count'] == len(pods['items'])
    slices = read('endpointslices', '-l', f'kubernetes.io/service-name={service}')
    readiness = [endpoint.get('conditions', {}).get('ready')
                 for item in slices['items'] for endpoint in item.get('endpoints') or []]
    assert observations['read_endpoints']['readiness'] == {
        'true': readiness.count(True), 'false': readiness.count(False), 'unspecified': readiness.count(None)}
    print(f'service={service} endpoints={len(readiness)} pods={len(pods["items"])} verified_with_kubectl=true')


async def run(base_url, namespace, service):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=['--no-sandbox'])
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(type(error).__name__))
        await page.goto(base_url, wait_until='domcontentloaded')
        await open_rca(page)
        await page.locator('#rca-runtime').select_option('workswarm')
        await page.locator('#rca-ns').fill(namespace)
        await page.locator('#rca-query').fill('核对目标 Service、EndpointSlice 和 Pod，说明就绪情况。')
        for target in (service, 'ws-absent-' + uuid.uuid4().hex[:12]):
            await page.locator('#rca-ws-service').fill(target)
            async with page.expect_response(lambda r: r.url.endswith('/api/rca/run') and r.request.method == 'POST') as reply:
                await page.get_by_role('button', name='开始分析', exact=True).click()
            response = await reply.value
            assert response.ok, response.status
            run_id = (await response.json())['run_id']
            heading = page.locator('#rca-result-content h4').filter(has_text='WorkSwarm')
            await heading.wait_for(timeout=245000)
            record = await (await page.request.get(f'{base_url}/api/rca/{run_id}')).json()
            result = record['result']
            assert result['service'] == target and result['namespace'] == namespace
            assert result['automation_gate']['allowed'] is False
            if target == service:
                assert result['workswarm_status'] == 'completed' and result['answer']
                verify_evidence(result['tool_evidence'], namespace, service)
            else:
                assert result['workswarm_status'] in ('partial', 'error') and 'read_service' in result['missing_tools']
            await page.reload(wait_until='domcontentloaded')
            await open_rca(page)
            await page.locator(f'[data-rca-run-id="{run_id}"]').click(timeout=20000)
            await heading.wait_for()
            assert await page.locator('#rca-ws-service').input_value() == target
            print(f'workswarm_status={result["workswarm_status"]} history_reload=passed healing=false')
        assert not errors, errors
        print('browser_errors=0')
        await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dashboard')
    parser.add_argument('namespace')
    parser.add_argument('service')
    args = parser.parse_args()
    asyncio.run(run(args.dashboard.rstrip('/'), args.namespace, args.service))
