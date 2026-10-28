"""Business continuity preview semantics in the real shell functions."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'node unavailable')
class ModuleTransitionUITests(unittest.TestCase):
    def run_js(self, assertions):
        html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        self.assertTrue('function moduleContinuityHTML(' in html, 'continuity renderer is missing')
        escape = re.search(r'^const esc =.*$', html, re.M).group(0)
        start = html.index('function moduleContinuityHTML(')
        end = html.index('// F-004:', start)
        source = escape + '\n' + html[start:end]
        script = source + '\n' + assertions
        with tempfile.TemporaryDirectory(prefix='wsflow-transition-ui-') as directory:
            file = Path(directory) / 'test.cjs'
            file.write_text(script, encoding='utf-8')
            result = subprocess.run([NODE, str(file)], capture_output=True,
                text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_report_text_and_data_are_escaped_and_unknown_is_not_zero(self):
        self.run_js(r'''
const assert=require('assert/strict');
const html=moduleContinuityHTML({ready:false,affected_modules:['work-calendar'],
 stores:[{store_id:'<img src=x onerror=1>',status:'unknown',pending_count:null}],
 blockers:[{message:'<script>bad()</script>'}]});
assert.ok(!html.includes('<img src=x'));
assert.ok(!html.includes('<script>bad'));
assert.ok(html.includes('&lt;img'));
assert.ok(html.includes('&lt;script&gt;'));
assert.ok(html.includes('확인 불가'));
assert.ok(html.includes('데이터 이관·복구'));
''')

    def test_known_pending_count_is_visible_when_only_pending_state_blocks(self):
        self.run_js(r'''
const assert=require('assert/strict');
const html=moduleContinuityHTML({ready:false,affected_modules:['work-calendar'],
 stores:[{store_id:'ops:synthetic',status:'blocked',pending_count:2}],
 blockers:[{store_id:'ops:synthetic',code:'pending_operations',message:'미처리 작업'}]});
assert.ok(html.includes('<td>2</td>'));
assert.ok(!html.includes('<td>확인 불가</td>'));
''')

    def test_blocked_preview_disables_reservation_and_shows_reason(self):
        self.run_js(r'''
const assert=require('assert/strict');
const nodes={'#moduleStatus':{},'#modulePlan':{},'#moduleApply':{},'#moduleCancel':{}};
const $=s=>nodes[s];
const api=async()=>({action:'update',module_id:'work-calendar',changes:[],plan_id:'MP-x',can_apply:false,
 continuity:{ready:false,affected_modules:['work-calendar'],stores:[],blockers:[{message:'미처리 승인 작업'}]}});
(async()=>{await previewModuleChange({});
assert.match(nodes['#modulePlan'].innerHTML,/<button id="moduleApply" disabled>/);
assert.ok(nodes['#modulePlan'].innerHTML.includes('미처리 승인 작업'));
assert.ok(nodes['#moduleStatus'].textContent.includes('예약할 수 없습니다'));})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_clear_preview_keeps_restart_and_preflight_scope_visible(self):
        self.run_js(r'''
const assert=require('assert/strict');
const nodes={'#moduleStatus':{},'#modulePlan':{},'#moduleApply':{},'#moduleCancel':{}};
const $=s=>nodes[s];
const api=async()=>({action:'update',module_id:'work-calendar',changes:[],plan_id:'MP-x',can_apply:true,
 continuity:{ready:true,affected_modules:['work-calendar'],stores:[],blockers:[]}});
(async()=>{await previewModuleChange({});
assert.ok(!nodes['#modulePlan'].innerHTML.includes('id="moduleApply" disabled'));
assert.ok(nodes['#modulePlan'].innerHTML.includes('재시작 직전'));
assert.ok(nodes['#modulePlan'].innerHTML.includes('자동 이관 없음'));})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_reservation_rejection_requires_new_preview_instead_of_reenabling_stale_plan(self):
        self.run_js(r'''
const assert=require('assert/strict');
const nodes={'#moduleStatus':{},'#modulePlan':{},'#moduleApply':{},'#moduleCancel':{}};
const $=s=>nodes[s];let calls=0;
const api=async()=>{if(calls++)throw Error('자료가 바뀌었습니다');
return {action:'update',module_id:'work-calendar',changes:[],plan_id:'MP-x',can_apply:true,
continuity:{ready:true,affected_modules:[],stores:[],blockers:[]}};};
(async()=>{await previewModuleChange({});await nodes['#moduleApply'].onclick();
assert.equal(nodes['#moduleApply'].disabled,true);
assert.ok(nodes['#moduleStatus'].textContent.includes('새로 미리보기'));})().catch(e=>{console.error(e);process.exitCode=1;});
''')
