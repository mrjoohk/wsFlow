import unittest
from test_ui import INDEX, NODE, _node


@unittest.skipUnless(NODE, 'node unavailable')
class CalendarPureTests(unittest.TestCase):
    def js(self, body):
        html=INDEX.read_text(encoding='utf-8');start=html.index('<script id="calendar-pure">')
        source=html[html.index('>',start)+1:html.index('</script>',start)]
        result=_node(source+'\n'+body);self.assertEqual(result.returncode,0,result.stderr)

    def test_month_end_leap_year_and_supported_boundaries(self):
        self.js('''
        const assert=require('assert/strict');
        assert.equal(calendarMove('2026-01-31','month',1),'2026-02-28');
        assert.equal(calendarMove('2024-01-31','month',1),'2024-02-29');
        assert.equal(calendarMove('2026-03-31','month',-1),'2026-02-28');
        assert.equal(calendarMove('2000-01-01','week',-1),'2000-01-01');
        assert.equal(calendarMove('2099-12-31','month',1),'2099-12-31');
        ''')

    def test_range_random_calendar_dates_are_contained_and_week_aligned(self):
        self.js('''
        const assert=require('assert/strict');
        for(let i=0;i<300;i++) {
          const date=calendarISO(new Date(Date.UTC(2000+(i*37)%100,(i*7)%12,1+(i*13)%28)));
          for(const mode of ['month','week']){
            const r=calendarRange(date,mode);
            assert.ok(r.start<=date&&date<r.end);assert.ok(r.dates.includes(date));
            assert.equal(r.dates.length%7,0);
            assert.equal(new Date(r.dates[0]+'T00:00Z').getUTCDay(),1);
            assert.ok(r.start>='2000-01-01'&&r.end<='2100-01-01');
          }
        }
        ''')

    def test_cells_only_have_event_preview_and_task_memo_counts(self):
        self.js('''
        const assert=require('assert/strict'),day={events:[1,2,3,4,5],task_count:2,memo_count:1,
          tasks:[{text:'PRIVATE TASK BODY'}],memos:[{body:'PRIVATE NOTE BODY'}]};
        const c=calendarCell(day);assert.deepEqual(c,{events:[1,2,3],overflow:2,task_count:2,memo_count:1});
        assert.ok(!JSON.stringify(c).includes('PRIVATE'));
        ''')

    def test_late_response_foreign_filter_and_left_screen_are_rejected(self):
        self.js('''
        const assert=require('assert/strict'),C={requestRevision:2,queryKey:'month/a/local'};
        assert.equal(calendarCurrent(C,1,C.queryKey,true),false);
        assert.equal(calendarCurrent(C,2,'month/b/local',true),false);
        assert.equal(calendarCurrent(C,2,C.queryKey,false),false);
        assert.equal(calendarCurrent(C,2,C.queryKey,true),true);
        ''')
