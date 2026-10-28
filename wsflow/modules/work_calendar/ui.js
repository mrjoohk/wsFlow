// Work/calendar views; loaded only for the active trusted package.
const CAL = { mode:'month',selected:null,projectId:'',source:'',timezone:'Asia/Seoul',requestRevision:0,queryKey:null,data:null,error:null,drafts:new Map(),editor:null };
const GCAL = {busy:false,status:null,calendars:[],error:null,timer:null,authTimer:null,outbox:{links:[],operations:[]},pending:null,revision:0,planRevision:0};
async function googleAction(action,body={}) {
  if(GCAL.busy)return;
  GCAL.busy=true;GCAL.error=null;GCAL.revision++;GCAL.planRevision++;drawGoogle();
  try {
    if(action==='connect') {
      const result=await api('POST','/api/v2/calendar/google/connect',body);
      if(calendarVisible()) {
        const box=$('#gAuthorization');box.replaceChildren();
        const link=document.createElement('a');link.href=result.authorization_url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Google 브라우저 승인 열기 (3분 이내)';box.append(link);
      }
      clearInterval(GCAL.authTimer);
      GCAL.authTimer=setInterval(async()=>{
        if(GCAL.busy)return;
        if(!calendarVisible()){clearInterval(GCAL.authTimer);return;}
        try {const s=await api('GET','/api/v2/calendar/google/status');GCAL.status=s;
          if(!s.pending){clearInterval(GCAL.authTimer);GCAL.error=s.auth_error;await refreshGoogle();if(calendarVisible())$('#gAuthorization').replaceChildren();await loadCalendar();}
        } catch(e){GCAL.error=e.message;clearInterval(GCAL.authTimer);}drawGoogle();
      },2000);
    } else {
      const result=await api('POST','/api/v2/calendar/google/'+action,body);
      GCAL.error=result.warning||(!result.complete&&action==='sync'?'일부 Google 캘린더를 확인하지 못했습니다. 이전 결과와 ? 표시를 확인하십시오.':null);
      if(action==='select') {
        const synced=await api('POST','/api/v2/calendar/google/sync',{});
        if(!synced.complete)GCAL.error='Google 수집 미완료 — 이전 결과와 ? 표시를 확인하십시오.';
      }
      await refreshGoogle();await loadCalendar();
    }
  } catch(e){GCAL.error=e.message;}
  finally{GCAL.busy=false;drawGoogle();}
}
async function refreshGoogle() {
  const ticket=++GCAL.revision;
  try {
    const status=await api('GET','/api/v2/calendar/google/status');
    const values=status.connected?await Promise.allSettled([api('GET','/api/v2/calendar/google/calendars'),api('GET','/api/v2/calendar/google/outbox')]):[];
    if(ticket!==GCAL.revision)return;
    const sameAccount=GCAL.status?.account?.id===status.account?.id;GCAL.status=status;
    GCAL.calendars=values[0]?.status==='fulfilled'?values[0].value.calendars:[];
    GCAL.outbox=values[1]?.status==='fulfilled'?values[1].value:(sameAccount&&status.connected?GCAL.outbox:{links:[],operations:[]});
    const failed=values.find(v=>v.status==='rejected');if(failed)GCAL.error=failed.reason.message;
  } catch(e){if(ticket===GCAL.revision)GCAL.error=e.message;}drawGoogle();
}
function drawGoogle() {
  const box=$('#gControls');if(!box||!calendarVisible())return;
  const s=GCAL.status,disabled=GCAL.busy?'disabled':'';
  box.innerHTML=`<p>${!s?'Google 연결 확인 중…':s.connected?'연결: '+esc(s.account.email)+' · 마지막 수집 '+esc(s.last_sync||'미수집')+' · '+(s.complete?'수집 확인':'수집 확인 필요'):'Google 미연결'}</p>
    ${GCAL.error||s?.background_error?`<p role="alert" class="warn">${esc(GCAL.error||s.background_error)}</p>`:''}
    ${s?.setup_error?`<p class="muted">${esc(s.setup_error)} · Google Cloud에서 Calendar API 사용 설정과 Desktop 앱 OAuth client를 준비하십시오.</p>`:''}
    <button id="gConnect" ${disabled} ${s&&!s.configured?'disabled':''}>${s?.connected?'계정 다시 연결':'Google 연결'}</button>
    ${s?.connected?`<button id="gDisconnect" ${disabled}>Google 연결 해제</button><button id="gSync" ${disabled}>Google 지금 동기화</button>
    ${!s.can_write?`<button id="gWrite" ${disabled}>Google 쓰기 권한 연결</button>`:''}
    <p class="muted">표시할 캘린더를 선택하십시오. 연결한 로컬 일정은 쓰기 권한이 있으면 동기화합니다. 외부 일정은 읽기 전용입니다. 앱 실행 중 5분마다 확인합니다.</p>
    <div>${GCAL.calendars.map((c,i)=>`<label style="display:block"><input type="checkbox" data-gcal="${i}" ${s.selected_calendar_ids.includes(c.id)?'checked':''} ${!c.can_read?'disabled':disabled}> ${esc(c.summary||c.id)} · ${esc(c.accessRole||'권한 미확인')}${!c.can_read?' (일정 내용 조회 불가)':''}</label>`).join('')}</div><button id="gSelect" ${disabled}>선택 저장·동기화</button>`:''}`;
  $('#gConnect').onclick=()=>googleAction('connect');
  if(s?.connected){$('#gDisconnect').onclick=()=>googleAction('disconnect');$('#gSync').onclick=()=>googleAction('sync');
    if($('#gWrite'))$('#gWrite').onclick=()=>googleAction('connect',{write:true});
    $('#gSelect').onclick=()=>googleAction('select',{calendar_ids:[...box.querySelectorAll('[data-gcal]:checked')].map(el=>GCAL.calendars[+el.dataset.gcal].id)});}
  drawGoogleOutbox();
}
function googleState(value){return ({local_only:'로컬만',queued:'전송 대기',syncing:'전송 중 · 재시작 후 대조',synced:'동기화됨',error:'전송 오류',conflict:'충돌 · 양쪽 확인',uncertain:'결과 미확인 · 원격 대조 필요',cancelled:'취소된 전송'})[value]||value;}
function drawGoogleOutbox(){
  const box=$('#gOutbox');if(!box)return;
  const links=GCAL.outbox.links||[],ops=GCAL.outbox.operations||[];
  box.innerHTML=`${GCAL.pending?'<p class="warn">승인 응답을 확인 중입니다. <button id="gPending">같은 승인 결과 확인</button></p>':''}
    ${links.length?'<h3>Google 연결 일정·복구</h3>':''}${links.map((l,i)=>{const op=ops.find(o=>o.id===l.pending),c=l.conflict;
      return `<div class="cal-entry"><b>${esc(l.local?.title||l.local_id)}</b> · ${esc(l.calendar_id)} · ${esc(googleState(l.state))}${l.hidden?' · 화면에서 숨김':''}
        ${l.remote_deleted?'<p class="warn">Google 원본이 삭제/취소됐습니다. 다시 표시는 로컬 보관 내용만 보여줍니다.</p>':''}
        ${op?.error?`<p class="warn">${esc(op.error)}</p>`:''}
        ${op&&['queued','error'].includes(op.state)?`<button data-gsend="${i}">같은 작업 전송</button>`:''}
        ${op&&['syncing','uncertain'].includes(op.state)?`<button data-greconcile="${i}">원격 결과 대조</button>`:''}
        ${c?`<p class="warn">${esc(c.reason)}</p><div style="white-space:pre-wrap"><b>로컬 내용</b><p>${esc(l.local?.title||'')}\n${esc(l.local?.body||'')}</p><b>Google 내용</b><p>${esc(c.remote?.summary||'원격 삭제/취소')}\n${esc(c.remote?.description||'')}</p></div>
        <button data-gresolve="${i}" data-choice="remote">Google 내용 채택 확인</button>${c.remote&&c.remote.status!=='cancelled'?`<button data-gresolve="${i}" data-choice="local">로컬 내용 Google 반영 확인</button>`:''}`:''}
        ${l.hidden?`<button data-gshow="${i}">화면에 다시 표시 확인</button>`:''}
        ${l.base_etag&&!l.remote_deleted?`<button class="danger" data-gdelete="${i}">Google에서 삭제 내용 확인</button>`:''}</div>`;}).join('')}`;
  if($('#gPending'))$('#gPending').onclick=()=>googleWriteApply();
  box.querySelectorAll('[data-gsend]').forEach(b=>b.onclick=()=>googleAction('outbox/run',{operation_id:links[+b.dataset.gsend].pending}));
  box.querySelectorAll('[data-greconcile]').forEach(b=>b.onclick=()=>googleAction('outbox/reconcile',{operation_id:links[+b.dataset.greconcile].pending}));
  box.querySelectorAll('[data-gresolve],[data-gshow],[data-gdelete]').forEach(b=>b.onclick=()=>{
    const i=+(b.dataset.gresolve??b.dataset.gshow??b.dataset.gdelete),l=links[i];
    googleWritePlan({action:b.dataset.gresolve!==undefined?'resolve':b.dataset.gshow!==undefined?'show':'delete',local_id:l.local_id,expected_revision:l.local.revision,
      ...(b.dataset.gresolve!==undefined?{resolution:b.dataset.choice,remote_etag:l.conflict.remote?.etag??null}:{})});
  });
}
async function googleWritePlan(request){
  if(GCAL.pending){GCAL.error='먼저 같은 승인 결과를 확인하십시오.';$('#gPanel').open=true;drawGoogle();return;}
  const ticket=++GCAL.planRevision;
  try{
    const plan=await api('POST','/api/v2/calendar/google/write/plan',request),dlg=$('#dlg');
    if(ticket!==GCAL.planRevision||!calendarVisible())return;
    dlg.onclose=null;dlg.innerHTML=`<h3>Google 일정 변경 확인</h3><p>${esc(request.action==='delete'?'Google 원본을 삭제합니다.':request.action==='publish'?'로컬 일정을 Google에 추가합니다.':request.action==='show'?'숨긴 일정을 이 화면에 다시 표시합니다.':request.resolution==='remote'?'Google 내용을 로컬에 채택합니다.':'로컬 내용을 Google에 반영합니다.')}</p><p>${esc(plan.item.destination.calendar_id)}</p><p>${esc(plan.item.remote?.summary||'')} ${esc(plan.item.remote?.description||'')}</p><button id="gApprove" class="danger">이 변경 승인</button><button id="gCancel">닫기</button>`;
    $('#gApprove').onclick=()=>{GCAL.pending=plan;dlg.close();googleWriteApply();};$('#gCancel').onclick=()=>dlg.close();if(!dlg.open)dlg.showModal();
  }catch(e){GCAL.error=e.message;$('#gPanel').open=true;drawGoogle();}
}
async function googleWriteApply(){
  if(!GCAL.pending||GCAL.busy)return;GCAL.busy=true;const plan=GCAL.pending;
  try{await api('POST','/api/v2/calendar/google/write/apply',{plan_id:plan.plan_id,operation_id:plan.operation_id});GCAL.pending=null;GCAL.error=null;}
  catch(e){GCAL.error=e.message;if(e.status&&e.status<500)GCAL.pending=null;}
  finally{GCAL.busy=false;await refreshGoogle();if(calendarVisible()){$('#gPanel').open=true;await loadCalendar();}}
}
function googlePublish(record){
  const choices=GCAL.calendars.filter(c=>c.can_write&&GCAL.status?.selected_calendar_ids.includes(c.id)),dlg=$('#dlg');
  if(!choices.length){GCAL.error='먼저 Google 쓰기 권한을 연결하고 쓸 수 있는 캘린더를 선택하십시오.';$('#gPanel').open=true;drawGoogle();return;}
  dlg.onclose=null;dlg.innerHTML=`<h3>Google에 추가할 일정</h3><p>${esc(record.title)}</p><label>대상 캘린더 <select id="gPublishTarget">${choices.map((c,i)=>`<option value="${i}">${esc(c.summary||c.id)}</option>`).join('')}</select></label><button id="gPublishPreview">추가 내용 확인</button><button id="gPublishCancel">닫기</button>`;
  $('#gPublishPreview').onclick=()=>googleWritePlan({action:'publish',local_id:record.id,expected_revision:record.revision,calendar_id:choices[+$('#gPublishTarget').value].id});
  $('#gPublishCancel').onclick=()=>dlg.close();if(!dlg.open)dlg.showModal();
}
function calendarTime(value) { return new Intl.DateTimeFormat('ko-KR',{timeZone:CAL.timezone,hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)); }
function calendarInputTime(value) { return calendarDateAt(value)+'T'+new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value)); }
function calendarNames(ids, unavailable=[]) { return ids.length ? ids.map(id=>unavailable.includes(id)?id+' (소속 확인 불가 · 과거 연결 보존)':(state.projects.find(p=>p.id===id)||{alias:id+' (등록 해제)'}).alias).join(', ') : '프로젝트 미연결'; }
function calendarVisible() { return state.view==='calendar' && !!$('#calendarRoot'); }
function calendarDrafts() {
  const box=$('#cDrafts');if(!box)return;
  const drafts=[...CAL.drafts.entries()].filter(([,d])=>d.dirty||d.pending);
  box.innerHTML=drafts.length?`<p class="warn">미저장 초안 ${drafts.length}개 — ${drafts.map(([key,d],i)=>`<button data-draft="${i}">${esc(d.payload.date||d.payload.start_date||(d.payload.start_at||'').slice(0,10))} ${d.kind==='event'?'일정':'메모'} 이어쓰기</button>`).join(' ')}</p>`:'';
  box.querySelectorAll('[data-draft]').forEach(b=>b.onclick=()=>openCalendarEditor(drafts[+b.dataset.draft][1].kind,null,drafts[+b.dataset.draft][0]));
}
async function renderCalendar() {
  state.view='calendar';state.current=null;renderList();$('#btnDash').classList.remove('on');$('#btnCalendar').classList.add('on');
  CAL.selected=CAL.selected||calendarDateAt();
  $('#content').innerHTML=`<div id="calendarRoot"><h2 style="margin:.1rem 0">캘린더</h2><p class="muted">저장된 업무 기록과 일정 · 표시 시간대 Asia/Seoul</p>
    <details id="gPanel"><summary>Google 연결·캘린더 선택</summary><div id="gAuthorization"></div><div id="gControls"></div><div id="gOutbox"></div></details>
    <div class="calendar-toolbar"><button id="cPrev" aria-label="이전 기간">←</button><button id="cToday">오늘</button><button id="cNext" aria-label="다음 기간">→</button><strong id="cPeriod"></strong>
    <label>보기 <select id="cMode"><option value="month">월</option><option value="week">주</option></select></label>
    <label>프로젝트 <select id="cProject"><option value="">전체 프로젝트</option>${state.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></label>
    <label>출처 <select id="cSource"><option value="">전체 기록</option><option value="local">직접 등록 일정·메모</option><option value="legacy_daily">기존 일지</option><option value="google">Google 일정</option></select></label><button id="cReload">새로 읽기</button></div>
    <div id="cDrafts"></div><p id="cStatus" role="status" aria-live="polite"></p><div class="calendar-layout"><div id="cGrid" role="grid" aria-label="날짜 캘린더"></div><div id="cDetail" class="cal-detail" aria-live="polite"></div></div></div>`;
  $('#cMode').value=CAL.mode;$('#cProject').value=CAL.projectId;$('#cSource').value=CAL.source;
  $('#cMode').onchange=e=>{CAL.mode=e.target.value;loadCalendar();};
  $('#cProject').onchange=e=>{CAL.projectId=e.target.value;loadCalendar();};$('#cSource').onchange=e=>{CAL.source=e.target.value;loadCalendar();};
  $('#cPrev').onclick=()=>{CAL.selected=calendarMove(CAL.selected,CAL.mode,-1);loadCalendar();};
  $('#cNext').onclick=()=>{CAL.selected=calendarMove(CAL.selected,CAL.mode,1);loadCalendar();};
  $('#cToday').onclick=()=>{CAL.selected=calendarDateAt();loadCalendar();};$('#cReload').onclick=()=>loadCalendar();
  clearInterval(GCAL.timer);
  GCAL.timer=setInterval(()=>{if(!calendarVisible()){clearInterval(GCAL.timer);return;}if(GCAL.status?.connected&&!GCAL.busy)googleAction('sync');},300000);
  calendarDrafts();refreshGoogle();await loadCalendar();
}
async function loadCalendar(focus=false) {
  if(!calendarVisible())return;
  const range=calendarRange(CAL.selected,CAL.mode), params=new URLSearchParams({start:range.start,end:range.end,timezone:CAL.timezone});
  if(CAL.projectId)params.append('project_id',CAL.projectId);if(CAL.source)params.append('source',CAL.source);
  const key=CAL.queryKey=params.toString(),ticket=++CAL.requestRevision;CAL.data=null;CAL.error=null;drawCalendar();
  try {
    const result=await api('GET','/api/v2/calendar/range?'+key);
    if(!calendarCurrent(CAL,ticket,key,calendarVisible()))return;
    CAL.data=result;drawCalendar();if(focus)$(`[data-date="${CAL.selected}"]`)?.focus();
  } catch(e) { if(calendarCurrent(CAL,ticket,key,calendarVisible())){CAL.error=e.message;drawCalendar();} }
}
function drawCalendar() {
  if(!calendarVisible())return;
  const range=calendarRange(CAL.selected,CAL.mode),today=calendarDateAt(), byDay=new Map((CAL.data?.days||[]).map(d=>[d.date,d]));
  $('#cPeriod').textContent=CAL.mode==='month'?CAL.selected.slice(0,7):range.start+' ~ '+calendarAdd(range.end,-1);
  $('#cPrev').disabled=calendarMove(CAL.selected,CAL.mode,-1)===CAL.selected;$('#cNext').disabled=calendarMove(CAL.selected,CAL.mode,1)===CAL.selected;
  $('#cStatus').textContent=CAL.error?'확인 불가: '+CAL.error:!CAL.data?'기록을 읽는 중…':CAL.data.complete?'저장된 기록을 확인했습니다.':'일부 기록을 확인하지 못했습니다. ?는 확인이 필요한 건수입니다.';
  if(CAL.data){const sources=CAL.data.sources.filter(s=>s.kind==='google');if(sources.length)$('#cStatus').textContent+=' Google: '+sources.map(s=>s.name+' · '+(s.complete?'확인':'확인 필요')+' · '+(s.last_sync||'미수집')).join(' / ');}
  const unavailable=new Set((CAL.data?.days||[]).flatMap(d=>(d.project_link_warnings||[]).map(w=>w.record_id)));
  if(unavailable.size)$('#cStatus').textContent+=` 소속을 확인할 수 없는 기록 ${unavailable.size}개는 프로젝트 필터에 자동 포함하지 않습니다. 전체 프로젝트 조회에서 확인·재연결할 수 있습니다.`;
  const cell=date=>{
    if(date<'2000-01-01'||date>'2099-12-31')return '<div role="gridcell" aria-disabled="true"></div>';
    const day=byDay.get(date),view=day?calendarCell(day):null,selected=date===CAL.selected;
    return `<button class="cal-day ${date.slice(0,7)!==CAL.selected.slice(0,7)?'outside':''} ${date===today?'today':''}" role="gridcell" data-date="${date}" aria-selected="${selected}" tabindex="${selected?0:-1}" aria-label="${date} 일정 ${day?.event_count??'?'} · 업무 ${day?.task_count??'?'} · 메모 ${day?.memo_count??'?'}"><span class="cal-date">${+date.slice(-2)}${date===today?' · 오늘':''}</span>
      ${view?view.events.map(e=>`<span class="cal-event">${e.all_day?'종일':calendarDateAt(e.start_at)!==date?'이어짐':calendarTime(e.start_at)} ${esc(e.title)}</span>`).join(''):''}
      ${view?.overflow?`<span class="muted">일정 +${view.overflow}</span>`:''}<span class="cal-counts">업무 ${day?.task_count??'?'} · 메모 ${day?.memo_count??'?'}</span></button>`;
  };
  let rows='<div class="cal-week" role="row">'+['월','화','수','목','금','토','일'].map(d=>`<span role="columnheader">${d}</span>`).join('')+'</div>';
  for(let i=0;i<range.dates.length;i+=7)rows+='<div class="cal-week" role="row">'+range.dates.slice(i,i+7).map(cell).join('')+'</div>';
  $('#cGrid').innerHTML=rows;
  $('#cGrid').querySelectorAll('[data-date]').forEach(button=>{
    button.onclick=()=>selectCalendarDate(button.dataset.date);
    button.onkeydown=e=>{const moves={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7};if(!(e.key in moves))return;e.preventDefault();selectCalendarDate(calendarClamp(calendarAdd(button.dataset.date,moves[e.key])));};
  });
  const day=byDay.get(CAL.selected),detail=$('#cDetail');
  const entry=(body,meta)=>`<div class="cal-entry">${body}<small class="muted">${esc(meta)}</small></div>`;
  detail.innerHTML=`<h2 style="margin:0;font-size:1.1rem">${esc(CAL.selected)}</h2><div class="actions"><button id="cAddEvent">일정 추가</button><button id="cAddNote">메모 추가</button></div>
    ${!day?`<p class="muted">${CAL.error?'기록을 확인할 수 없습니다.':'기록을 읽는 중입니다.'}</p>`:`${!day.complete?'<p class="warn">일부 출처 확인 불가</p>':''}
    <h3>일정 ${day.event_count??'?'}</h3>${day.events.map(e=>entry(`<b>${esc(e.title)}</b><p>${e.all_day?esc(e.start_date+' ~ '+e.end_date+' (종료일 제외)'):esc(calendarDateAt(e.start_at)+' '+calendarTime(e.start_at)+' ~ '+calendarDateAt(e.end_at)+' '+calendarTime(e.end_at))}</p>${e.location?`<p>${esc(e.location)}</p>`:''}<p>${esc(e.body)}</p>${e.origin==='google'?'<span class="muted">Google 읽기 전용</span>':`<button data-edit="${esc(e.id)}">수정·삭제</button>${e.google_link?`<p class="muted">Google · ${esc(e.google_link.calendar_id)} · ${esc(googleState(e.sync_status))}</p>`:`<button data-gpublish="${esc(e.id)}">Google에 추가</button>`}`}`,e.origin==='google'?'Google · '+e.calendar_name+' · '+e.access_role:calendarNames(e.project_ids,e.unavailable_project_ids)+(e.google_link?' · Google 연결 일정':' · 로컬에만 저장'))).join('')||'<p class="muted">확인된 일정이 없습니다.</p>'}
    <h3>수행 업무 ${day.task_count??'?'}</h3>${day.tasks.map(t=>entry(`<b>${esc(t.summary||t.key)}</b>${t.records.map(r=>`<p>${esc(r.text)}</p>`).join('')}`,calendarNames(t.project_ids)+' · 기존 일지')).join('')||'<p class="muted">저장된 업무 기록이 없습니다.</p>'}
    <h3>메모 ${day.memo_count??'?'}</h3>${day.memos.map(m=>entry(`<p>${esc(m.kind==='note'?m.body:[...(m.common||[]),...(m.marks||[]).map(x=>x.mark+' '+x.text)].join('\n'))}</p>${m.kind==='note'?`<button data-edit="${esc(m.id)}">수정·삭제</button>`:''}`,calendarNames(m.project_ids,m.unavailable_project_ids)+(m.kind==='note'?' · 로컬 메모':' · 기존 공통 메모 묶음'))).join('')||'<p class="muted">저장된 메모가 없습니다.</p>'}`}`;
  $('#cAddEvent').onclick=()=>openCalendarEditor('event');$('#cAddNote').onclick=()=>openCalendarEditor('note');
  detail.querySelectorAll('[data-gpublish]').forEach(b=>b.onclick=()=>googlePublish(day.events.find(e=>e.id===b.dataset.gpublish)));
  detail.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const r=[...day.events,...day.memos].find(r=>r.id===b.dataset.edit);openCalendarEditor(r.kind,r);});calendarDrafts();
}
function selectCalendarDate(date) {
  CAL.selected=date;const range=calendarRange(date,CAL.mode);
  if(!CAL.data||range.start!==CAL.data.query.start||range.end!==CAL.data.query.end)loadCalendar(true);
  else {drawCalendar();$(`[data-date="${date}"]`)?.focus();}
}
function openCalendarEditor(kind, record=null, savedKey=null) {
  const key=savedKey||(record?record.id:`new:${kind}:${CAL.selected}`);
  if(!CAL.drafts.has(key))CAL.drafts.set(key,{kind,id:record?.id||null,expected_revision:record?.revision||null,unavailable:record?.unavailable_project_ids||[],rebind:[],revision:0,dirty:false,plan:null,pending:null,destination:'',googleLink:record?.google_link||null,
    payload:record?Object.fromEntries(Object.entries(record).filter(([key])=>['title','body','location','project_ids','all_day','start_date','end_date','start_at','end_at','timezone','date'].includes(key))):kind==='note'?{date:CAL.selected,body:'',project_ids:CAL.projectId?[CAL.projectId]:[]}:{title:'',body:'',location:'',all_day:true,start_date:CAL.selected,end_date:calendarAdd(CAL.selected,1),project_ids:CAL.projectId?[CAL.projectId]:[]}});
  const d=CAL.drafts.get(key),p=d.payload,dlg=$('#dlg');CAL.editor=key;
  const timedStart=p.start_at?calendarInputTime(p.start_at):(p.start_date||CAL.selected)+'T09:00',timedEnd=p.end_at?calendarInputTime(p.end_at):(p.start_date||CAL.selected)+'T10:00';
  const links=[...new Set([...state.projects.map(p=>p.id),...p.project_ids])];
  const destinations=GCAL.calendars.filter(c=>c.can_write&&GCAL.status?.selected_calendar_ids.includes(c.id));
  dlg.innerHTML=`<form id="calendarEditor"><h3>${d.id?'로컬 기록 수정':'로컬 '+(kind==='event'?'일정':'메모')+' 추가'}</h3><p class="muted">${d.googleLink?'Google 연결 일정입니다. 저장한 변경은 연결된 캘린더로 전송합니다.':'로컬 저장 또는 Google 추가를 선택할 수 있습니다.'}</p>
    ${kind==='event'&&!d.id?`<label>저장 대상 <select id="cDestination"><option value="">로컬만</option>${destinations.map(c=>`<option value="${esc(c.id)}" ${d.destination===c.id?'selected':''}>Google · ${esc(c.summary||c.id)}</option>`).join('')}</select></label>`:''}
    ${kind==='event'?`<label>제목 <input data-field="title" value="${esc(p.title)}" required maxlength="500"></label><label><input type="checkbox" data-field="all_day" ${p.all_day?'checked':''}>종일 일정</label>
      <div id="cAllDay" class="cal-editor-times"><label>시작 날짜 <input type="date" data-field="start_date" value="${esc(p.start_date||calendarDateAt(p.start_at))}"></label><label>종료 날짜 (이 날 미포함) <input type="date" data-field="end_date" value="${esc(p.end_date||calendarDateAt(p.end_at))}"></label></div>
      <div id="cTimed" class="cal-editor-times"><label>시작 시각 <input type="datetime-local" data-field="start_at" value="${timedStart}"></label><label>종료 시각 <input type="datetime-local" data-field="end_at" value="${timedEnd}"></label><p class="muted">Asia/Seoul 기준. 시각을 수정하면 이 시간대로 저장합니다.</p></div><label>장소 <input data-field="location" value="${esc(p.location)}"></label>`:
      `<label>업무 날짜 <input type="date" data-field="date" value="${esc(p.date)}"></label>`}
    <label>본문 <textarea data-field="body" rows="5">${esc(p.body)}</textarea></label><label>프로젝트 연결 (복수 선택 가능) <select data-field="project_ids" multiple size="${Math.min(4,Math.max(2,links.length))}">${links.map(id=>`<option value="${esc(id)}" ${p.project_ids.includes(id)?'selected':''}>${esc(calendarNames([id],d.unavailable))}</option>`).join('')}</select></label>
    ${d.unavailable.filter(id=>state.projects.some(p=>p.id===id)).map(id=>`<label><input type="checkbox" data-rebind="${esc(id)}" ${(d.rebind||[]).includes(id)?'checked':''}>과거 연결을 현재 프로젝트 ${esc((state.projects.find(p=>p.id===id)||{}).alias||id)}에 다시 연결 — 저장 전 별도 확인</label>`).join('')}
    <div id="cEditorStatus" role="status"></div><div id="cPreview"></div><div class="actions"><button type="submit" class="primary" id="cSave">${d.pending?'같은 작업의 저장 결과 확인':'저장 내용 확인'}</button>${d.id?`<button type="button" id="cDelete" class="danger">${d.googleLink?'화면 숨김 내용 확인':'삭제 내용 확인'}</button>`:''}<button type="button" id="cClose">닫기 · 초안 유지</button><button type="button" id="cDiscard">초안 버리기</button></div></form>`;
  function toggle(){if(kind==='event'){$('#cAllDay').style.display=$('[data-field=all_day]',dlg).checked?'flex':'none';$('#cTimed').style.display=$('[data-field=all_day]',dlg).checked?'none':'flex';}}
  toggle();
  function collect(){
    const get=name=>$(`[data-field=${name}]`,dlg),links=[...get('project_ids').selectedOptions].map(o=>o.value);
    let payload={body:get('body').value,project_ids:links};
    if(kind==='note')payload.date=get('date').value;
    else {payload={...payload,title:get('title').value,location:get('location').value,all_day:get('all_day').checked};
      if(payload.all_day){payload.start_date=get('start_date').value;payload.end_date=get('end_date').value;}
      else if(p.start_at && get('start_at').value===timedStart && get('end_at').value===timedEnd){payload.start_at=p.start_at;payload.end_at=p.end_at;payload.timezone=p.timezone;}
      else {payload.start_at=get('start_at').value+':00+09:00';payload.end_at=get('end_at').value+':00+09:00';payload.timezone='Asia/Seoul';}}
    d.payload=payload;d.rebind=[...dlg.querySelectorAll('[data-rebind]:checked')].map(e=>e.dataset.rebind).filter(id=>links.includes(id));d.dirty=true;d.revision++;d.plan=null;$('#cPreview').innerHTML='';calendarDrafts();toggle();
    if($('#cDestination'))d.destination=$('#cDestination').value;
  }
  $('#calendarEditor').oninput=collect;
  $('#cClose').onclick=()=>dlg.close();$('#cDiscard').onclick=()=>{if(!d.pending){CAL.drafts.delete(key);dlg.close();calendarDrafts();}else $('#cEditorStatus').textContent='먼저 같은 작업의 저장 결과를 확인하십시오.';};
  dlg.onclose=()=>{CAL.editor=null;calendarDrafts();};
  async function preview(action){
    if(d.pending)return applyPending();
    const rev=d.revision;$('#cSave').disabled=true;
    try {const payload={action,kind,...(d.id?{id:d.id,expected_revision:d.expected_revision}:{}),...(action==='delete'?{}:{payload:d.payload}),...(action==='update'&&d.rebind?.length?{rebind_project_ids:d.rebind}:{}),...(!d.id&&d.destination?{destination:{calendar_id:d.destination}}:{})};
      const plan=await api('POST','/api/v2/calendar/plan',payload);
      if(CAL.editor!==key || rev!==d.revision)return;
      d.plan=plan;$('#cPreview').innerHTML=`<div class="panel"><b>${action==='delete'?(d.googleLink?'이 화면에서 숨깁니다. Google 원본은 유지합니다.':'이 기록을 로컬에서 삭제합니다.'):(d.destination||d.googleLink?'이 내용을 저장하고 Google에 반영합니다.':'이 내용을 로컬에 저장합니다.')}</b><p>${esc(plan.item.payload.title||plan.item.payload.body)}</p><p>${esc(plan.item.payload.date||plan.item.payload.start_date||plan.item.payload.start_at)} ${esc(plan.item.payload.end_date||plan.item.payload.end_at||'')}</p><button type="button" id="cApply" class="${action==='delete'?'danger':'primary'}">${action==='delete'?(d.googleLink?'화면에서 숨김':'로컬에서 삭제'):(d.destination||d.googleLink?'저장·Google 반영':'로컬에 저장')}</button></div>`;
      const changes=(plan.item.link_updates||[]).map(c=>`${({add:'연결 추가',remove:'연결 해제',rebind:'현재 프로젝트로 명시 재연결'})[c.action]}: ${(state.projects.find(p=>p.id===c.project_id)||{}).alias||c.project_id}${c.identity?.status==='legacy_path'?' (UID 미설정 · 폴더 경계 기준)':''}`);
      if(changes.length)$('#cApply').insertAdjacentHTML('beforebegin',`<p class="warn">${changes.map(esc).join('<br>')}</p>`);
      $('#cApply').onclick=()=>{d.pending={plan,revision:rev};applyPending();};
    }catch(e){if(d.revision===rev)showError(e);}finally{if(CAL.editor===key)$('#cSave').disabled=false;}
  }
  function showError(e){if(CAL.editor!==key)return;const box=$('#cEditorStatus');box.innerHTML=`<p class="bad">${esc(e.message)} — 입력은 초안에 보존됩니다.</p>`;
    if(e.data?.current){box.innerHTML+=`<details><summary>현재 저장본</summary><p style="white-space:pre-wrap">${esc(e.data.current.title||'')}\n${esc(e.data.current.body||'')}</p></details><button type="button" id="cRebase">현재 버전을 기준으로 다시 확인</button>`;
      $('#cRebase').textContent=e.data.current.deleted?'입력 내용을 새 기록으로 확인':'현재 버전을 기준으로 다시 확인';
      $('#cRebase').onclick=()=>{d.id=e.data.current.deleted?null:d.id;d.expected_revision=e.data.current.deleted?null:e.data.current.revision;d.plan=null;d.pending=null;box.textContent='현재 저장본과 비교해 내용을 조정한 뒤 저장 내용을 다시 확인하십시오.';};}
  }
  async function applyPending(){
    const pending=d.pending;if(!pending||d.applying)return;d.applying=true;
    if(CAL.editor===key){$('#cSave').disabled=true;$('#cApply')?.setAttribute('disabled','');}
    try {const result=await api('POST','/api/v2/calendar/apply',{plan_id:pending.plan.plan_id,operation_id:pending.plan.operation_id});
      d.pending=null;d.plan=null;
      if(d.revision===pending.revision){CAL.drafts.delete(key);if(CAL.editor===key)dlg.close();}
      else {d.id=result.record.deleted?null:result.record.id;d.expected_revision=result.record.deleted?null:result.record.revision;if(CAL.editor===key){$('#cPreview').innerHTML='';$('#cEditorStatus').textContent='저장 뒤 추가한 입력은 미저장 초안으로 남아 있습니다.';}}
      toast(result.google_operation_id?'일정을 저장했습니다. Google 전송 상태를 확인하십시오.':'로컬 기록을 저장했습니다');if(result.google_operation_id||d.googleLink)await refreshGoogle();if(calendarVisible())await loadCalendar();
    }catch(e){if(e.status && e.status<500)d.pending=null;showError(e);}
    finally{d.applying=false;if(CAL.editor===key){$('#cSave').disabled=false;$('#cSave').textContent=d.pending?'같은 작업의 저장 결과 확인':'저장 내용 확인';}calendarDrafts();}
  }
  $('#calendarEditor').onsubmit=e=>{e.preventDefault();preview(d.id?'update':'create');};
  if($('#cDelete'))$('#cDelete').onclick=()=>preview('delete');
  if(!dlg.open)dlg.showModal();
}
window.addEventListener('beforeunload',e=>{if([...CAL.drafts.values()].some(d=>d.dirty||d.pending)){e.preventDefault();e.returnValue='';}});
// ------------------------------------------------------------- Jira (S5)
// 메모는 유일하게 재생성 불가능한 원본이다(INV-1). 화면도 그 책임을 진다 —
// 열 때 받은 sha 를 그대로 돌려보내고, 서버가 409 를 주면 덮지 않고 사람에게 보인다.
const jiraState = new Map();   // 프로젝트 id → 화면 상태 (jiraFor)
let jiraYmd = null;            // 현재 선택. 초안·응답은 별도 문서 키에 결부한다.
let jiraRenderRevision = 0;
function jiraDoc(p) {
  const parent = jiraFor(jiraState, p.id);
  return jiraFor(jiraState, p.id, parent.ymd || jiraYmd || ymdToday(), (parent.info || {}).wf_home || p.path);
}
function jiraVisible(p, J) {
  const body = $("#jBody");
  return state.current === p.id && state.tab === "jira" && body && body.dataset.doc === J.documentKey;
}

function renderSaveState(J) {
  if (J.documentKey && (!$("#jBody") || $("#jBody").dataset.doc !== J.documentKey)) return;
  const el = $("#jSaveState"); if (!el) return;
  el.innerHTML = J.pendingMemo ? '<span class="warn">저장 결과 확인 필요 · 같은 작업 ID로 확인합니다</span>' : J.dirty ? '<span class="warn">● 수정됨 — 저장되지 않음</span>'
    : J.savedAt ? `<span class="ok">저장됨 ${esc(J.savedAt)}</span>` : "";
  const pl = $("#jPlan"); if (pl && !pl.disabled) pl.textContent = J.dirty ? "저장하고 마감 초안" : "마감 초안";
}
// 저장하지 않은 메모가 있으면 묻는다. true 면 계속 진행해도 된다.
async function guardJiraUnsaved(p) {
  const docs = unsavedDocuments(jiraState, p ? p.id : null);
  if (!docs.length) return true;
  const names = docs.map(({ id, J }) => (state.projects.find(x => x.id === id) || { alias: id }).alias + (J.ymd ? " / " + J.ymd : "")).join(", ");
  const v = await ask({
    title: "저장하지 않은 메모가 있습니다",
    body: `<p><b>${esc(names)}</b> 의 진행 메모에 저장하지 않은 입력이 있습니다. 계속하면 지금 입력한 내용이 사라집니다.</p>`,
    buttons: [{ label: "취소", value: "cancel" }, { label: "버리고 계속", value: "discard", kind: "danger" },
              { label: "저장하고 계속", value: "save", kind: "primary" }],
  });
  if (v === "save") {
    for (const { id, J } of docs) { if (!await saveMemo(state.projects.find(x => x.id === id), J)) return false; }
    return true;
  }
  if (v === "discard") { docs.forEach(({ J }) => { J.dirty = false; J.draft = null; J.editRevision++; }); return true; }
  return false;
}
window.addEventListener("beforeunload", e => { if (unsavedIds(jiraState).length) { e.preventDefault(); e.returnValue = ""; } });

async function renderJira(body, p) {
  const ticket = ++jiraRenderRevision, parent = jiraFor(jiraState, p.id);
  parent.ymd = parent.ymd || ymdToday(); jiraYmd = parent.ymd;
  const info = await api("GET", `/api/projects/${p.id}/jira`);
  if (ticket !== jiraRenderRevision || state.current !== p.id || state.tab !== "jira") return;
  parent.info = info;
  const J = jiraDoc(p); J.info = info;
  const warn = [];
  if (info.enabled && info.schema_ok === false) warn.push(info.reason);
  if (info.gitignore_ok === false) warn.push("이 저장소의 .gitignore 가 업무 데이터를 제외하지 않습니다 — .workflow/ · daily/ · weekly/ · state.json · jira_project_key_id.txt 를 추가하십시오. 없으면 업무 기록이 커밋됩니다.");
  const sc = info.scope || {};
  // 자격은 서버가 값 없이 상태만 준다(D-007). 없으면 조회 전에 여기서 보인다 — 412 toast 로 알 일이 아니다(UX-06).
  const cred = await api("GET", `/api/jira/status${info.site_url ? "?site=" + encodeURIComponent(info.site_url) : ""}`).catch(e => ({ configured: false, error: e.message }));
  if (ticket !== jiraRenderRevision || state.current !== p.id || state.tab !== "jira") return;
  const credLine = cred.configured
    ? `<span class="ok">● Jira 자격 있음</span> <span class="muted">${esc(cred.email || "")}${cred.source ? " · " + esc(cred.source === "env" ? "환경변수" : "credentials.json") : ""}${cred.warning ? " · " + esc(cred.warning) : ""}</span>`
    : `<span class="bad">○ Jira 자격 없음</span> <span class="muted">${esc(cred.error || "")}${cred.path ? ` — 파일 위치 <code>${esc(cred.path)}</code>${cred.path_exists ? " (파일은 있음: 키 이름을 확인)" : " (파일 없음)"}` : ""}</span>`;
  body.innerHTML = `
    <p><label><input type="checkbox" id="jiraOn" ${info.enabled ? "checked disabled" : ""}>
      Jira 워크플로 사용</label>
      ${info.enabled ? '<span class="chip">설정 파일이 있어 항상 켜집니다</span>' : `<span class="muted">${esc(info.reason)}</span>`}
      <button id="jInit">${info.enabled ? "설정 수정" : "설정 작성"}</button></p>
    <p id="jCred">${credLine}</p>
    <div id="jInitBox"></div>
    ${info.migration_available ? '<div class="box"><button id="jMigratePlan">보존형 설정 전환 미리보기</button><div id="jMigrateResult" role="status"></div></div>' : ''}
    ${info.enabled ? `<p class="muted">${esc(info.wf_home)} · 프로젝트 ${esc(sc.project_key || "?")}${sc.csc_value ? " · CSC " + esc(sc.csc_value) : ""}${(sc.epic_keys || []).length ? ` · 에픽 ${sc.epic_keys.length}개` : ""}</p>` : ""}
    ${warn.map(w => `<p class="warn">${esc(w)}</p>`).join("")}
    ${info.enabled ? `
      <p><label>날짜 <input id="jYmd" size="8" value="${esc(jiraYmd)}" title="YYMMDD"></label>
         <button id="jLoad">${J.view ? "다시 조회" : "조회"}</button>
         <span class="muted" id="jLoadedAt">${J.loadedAt ? `마지막 조회 ${esc(J.loadedAt)} · ` : ""}조회는 Jira 를 읽기만 합니다.</span></p>
      <div class="box"><button id="jBatch" ${parent.batchRunning?'disabled':''}>오늘 일일 갱신</button> <button id="jBatchStatus">배치 기록 확인</button>
        <span class="muted">Jira 계정 날짜로 조회·집계·일지·검사를 실행합니다. 검사 성공 후 watermark를 확정합니다. Jira에는 쓰지 않습니다.</span><div id="jBatchResult" role="status">${parent.batchResult?batchReceipt(parent.batchResult):''}</div></div>
      <div id="jBody" data-doc="${esc(J.documentKey)}">${J.view ? "" : '<p class="muted">이 폴더가 맡은 태스크와 메모를 불러옵니다…</p>'}</div>` : ""}`;
  $("#jInit").onclick = () => initPanel(p, info);
  if ($('#jMigratePlan')) {
    $('#jMigratePlan').onclick=()=>previewJiraMigration(p);
    if(parent.migration)drawJiraMigration(p);
  }
  // 꺼진 상태에서 체크하면 설정 작성으로 간다(D-004). 설정 파일이 생겨야 켜지므로 체크 표시는 유지하지 않는다.
  $("#jiraOn").onchange = e => { if (!info.enabled) { e.target.checked = false; initPanel(p, info); } };
  if (!info.enabled) return;
  $('#jBatch').onclick=()=>runJiraBatch(p);
  if(info.schema_ok === false)$('#jBatch').disabled=true;
  $('#jBatchStatus').onclick=()=>showJiraBatch(p);
  $("#jYmd").onchange = async e => {
    const next = e.target.value.trim();
    if (!await guardJiraUnsaved(p)) { e.target.value = jiraYmd; return; }   // 날짜를 바꾸면 메모가 바뀐다
    parent.ymd = next; jiraYmd = next;
    renderJira(body, p);
  };
  $("#jLoad").onclick = async () => { if (await guardJiraUnsaved(p)) loadJira(p); };
  if (J.view) drawJira(p);
  // 첫 진입이면 바로 읽는다 — 읽기 전용이고, 매번 「조회」를 누르게 하는 것은 클릭만 늘린다(UX-09).
  // 자격이 없으면 자동으로 부르지 않는다 — 위의 자격 줄이 이미 이유를 말하고 있다.
  else if (cred.configured) loadJira(p);
  else $("#jBody").innerHTML = '<p class="warn">Jira 자격이 없어 조회하지 않았습니다 — 위 안내대로 자격을 설정한 뒤 「조회」를 누르십시오.</p>';
}

function drawJiraMigration(p) {
  const box=$('#jMigrateResult'), parent=jiraFor(jiraState,p.id), plan=parent.migration;
  if(!box||!plan)return;
  box.innerHTML=`<p>설정 버전 ${esc(plan.from_schema)} → ${esc(plan.to_schema)}. 나머지 ${esc(plan.preserved_fields.length)}개 필드와 업무 파일을 보존하고 원본 bytes를 백업합니다.</p><button id="jMigrateApply">백업 후 버전만 전환</button> <button id="jMigrateCheck">전환 결과 확인</button><p id="jMigrateStatus" role="status"></p>`;
  $('#jMigrateApply').onclick=async()=>{
    const button=$('#jMigrateApply'),msg=$('#jMigrateStatus');button.disabled=true;
    try{
      if(!await guardJiraUnsaved(p))return;
      const r=await api('POST',`/api/projects/${p.id}/jira/migration/apply`,{plan_id:plan.plan_id,operation_id:plan.operation_id});
      parent.migration=null;
      if(state.current===p.id&&state.tab==='jira')await renderJira($('#tabBody'),p);
      toast('설정 버전을 전환했습니다. 원본 백업: '+r.backup);
    }catch(e){if(msg.isConnected)msg.textContent='전환 응답을 확인하지 못했습니다. 같은 계획의 결과를 확인하십시오. '+e.message;}
    finally{if(button.isConnected)button.disabled=false;}
  };
  $('#jMigrateCheck').onclick=async()=>{
    const msg=$('#jMigrateStatus');
    try{
      const r=await api('GET',`/api/projects/${p.id}/jira/migration?plan_id=${encodeURIComponent(plan.plan_id)}&operation_id=${encodeURIComponent(plan.operation_id)}`);
      if(msg.isConnected)msg.textContent=r.committed?'원본 백업과 전환된 설정을 확인했습니다. 새로고침해 현재 설정을 확인하십시오.':r.source_unchanged?'원본 설정이 유지됩니다. 동일 계획으로 다시 적용할 수 있습니다.':'설정이 변경되었거나 결과가 불확실합니다. 새 미리보기 전에 원본과 백업을 확인하십시오.';
    }catch(e){if(msg.isConnected)msg.textContent=e.message;}
  };
}
async function previewJiraMigration(p) {
  const box=$('#jMigrateResult');
  try{
    const plan=await api('POST',`/api/projects/${p.id}/jira/migration/plan`,{});
    jiraFor(jiraState,p.id).migration=plan;
    if(box.isConnected&&state.current===p.id&&state.tab==='jira')drawJiraMigration(p);
  }catch(e){if(box.isConnected)box.textContent=e.message;}
}

async function loadJira(p) {
  const J = jiraDoc(p), b = $("#jBody"), ticket = ++J.requestRevision, ymd = J.ymd;
  if (!jiraVisible(p, J)) return;
  b.innerHTML = '<p class="muted">Jira 조회 중…</p>';
  try {
    const [view, memo] = await Promise.all([
      api("GET", `/api/projects/${p.id}/jira/tasks`),
      api("GET", `/api/projects/${p.id}/jira/memo?ymd=${encodeURIComponent(ymd)}`),
    ]);
    if (!acceptJiraResponse(J, ticket, view, memo)) return;
    if (!J.pendingMemo && memo.common_writes?.length) J.pendingMemo = memo.common_writes[0];
    J.loadedAt = new Date().toTimeString().slice(0, 5);
    if (!jiraVisible(p, J)) return;
    const at = $("#jLoadedAt"); if (at) at.textContent = `마지막 조회 ${J.loadedAt} · 조회는 Jira 를 읽기만 합니다.`;
    const lb = $("#jLoad"); if (lb) lb.textContent = "다시 조회";
    drawJira(p);
  } catch (e) { if (jiraVisible(p, J) && ticket === J.requestRevision) { b.innerHTML = `<p class="warn">${esc(e.message)}</p>`; fail(e); } }
}

function batchReceipt(result) {
  if(!result)return '<p class="muted">실행 기록 없음 · watermark 미확인</p>';
  return `<p class="${result.status==='succeeded'?'ok':'warn'}">${esc(result.status)} · ${esc(result.date||'날짜 미확정')} · ${esc(result.run_id)}</p>
    <p>watermark ${esc(result.watermark_in||'?')} → ${esc(result.committed?result.watermark_out:'보존 · 전진하지 않음')}</p>
    <p>${(result.stages||[]).map(s=>esc(s.stage)+' ✓').join(' → ')}</p>
    ${result.error?`<p class="warn">${esc(result.failed_stage||'')} · ${esc(result.error)}</p>`:''}
    ${(result.support?.warnings||[]).map(w=>`<p class="warn">${esc(w)}</p>`).join('')}
    <p class="muted" style="overflow-wrap:anywhere">증거: ${esc(result.run_dir||'')}</p>`;
}
async function showJiraBatch(p) {
  const box=$('#jBatchResult');if(!box)return;
  try {const r=await api('GET',`/api/projects/${p.id}/jira/batch`);if(!box.isConnected||state.current!==p.id)return;
    box.innerHTML=batchReceipt(r.latest)+`<p>확정 watermark: ${esc(r.watermark||'미확인')}</p>`+(r.warnings||[]).map(w=>`<p class="warn">${esc(w)}</p>`).join('');if(r.support?.error)box.insertAdjacentHTML('beforeend',`<p class="warn">${esc(r.support.error)}</p>`);
  }catch(e){if(box.isConnected)box.textContent=e.message;}
}
async function runJiraBatch(p) {
  const parent=jiraFor(jiraState,p.id);if(parent.batchRunning)return;
  parent.batchRunning=true;const button=$('#jBatch'),box=$('#jBatchResult');button.disabled=true;
  try {
    if(!await guardJiraUnsaved(p))return;
    if(!box.isConnected)return;
    box.textContent='일일 갱신 중…';
    const r=await api('POST',`/api/projects/${p.id}/jira/batch/run`,{});parent.batchResult=r;
    if(!box.isConnected||state.current!==p.id||state.tab!=='jira')return;
    box.innerHTML=batchReceipt(r);
    if(r.status==='succeeded')await loadJira(p);
  }catch(e){if(box.isConnected)box.textContent='응답을 확인하지 못했습니다. 배치 기록 확인으로 최종 state marker를 확인하십시오. '+e.message;}
  finally{parent.batchRunning=false;if(button.isConnected)button.disabled=false;}
}

function badge(label, n, hint) {
  if (!n) return "";
  return `<span class="chip" title="${esc(hint)}">${esc(label)} ${n}</span>`;
}

function drawJira(p) {
  const J = jiraDoc(p), v = J.view, c = v.counts, split = splitMemo(memoTextFor(J));
  if (!jiraVisible(p, J)) return;
  const unpushed = v.tasks.reduce((a, t) => a + (t.children_unpushed || []).length, 0);
  const rows = v.tasks.map(t => `
    <tr><td><code>${esc(t.key)}</code>${t.root_id ? ` <span class="muted">${esc(t.root_id)}</span>` : ""}</td>
        <td>${esc(t.summary)}</td>
        <td>${esc(t.status)}</td>
        <td>${esc(t.parent_summary || "")}</td>
        <td><textarea data-key="${esc(t.key)}" rows="2" style="width:100%">${esc((split.by[t.key] || []).join("\n"))}</textarea></td></tr>`).join("");

  $("#jBody").innerHTML = `
    <p>
      ${badge("맡은 태스크", c.tasks, "이 폴더 범위 안의 미완료")}
      ${badge("목록 밖 CSU", c.epic_unlisted, "PM 이 새 에픽을 만들었을 수 있습니다 — 맡을 것이면 state.json 의 epic_keys 에 추가")}
      ${badge("제목 필터로 빠짐", c.title_filtered, "어느 폴더도 안 맡을 수 있습니다 — 제목 규약 확인")}
      ${badge("CSC 미지정", c.orphans, "CSC 는 에픽에서 상속되지 않습니다 — 지정하지 않으면 다음 조회에서 사라집니다")}
      ${badge("범위 밖", c.out_of_scope, "조회가 이 폴더 범위를 넘었습니다")}
      ${badge("안 올라간 파생", unpushed, "승인됐는데 아직 Jira 에 만들어지지 않았습니다")}
    </p>
    <div class="tblwrap"><table><tr><th>키</th><th>제목</th><th>상태</th><th>에픽</th><th style="min-width:16rem">${esc(jiraYmd)} 메모</th></tr>${rows || '<tr><td colspan="5" class="muted">맡은 미완료 태스크가 없습니다</td></tr>'}</table></div>
    <p style="margin-top:.8rem">그 밖의 메모 <span class="muted">(<code>- [신규] …</code> · <code>- [파생] …</code> · <code>- [BUG] …</code> 는 티켓 후보, <code>- [출처] …</code> 는 로컬에만 남습니다)</span></p>
    <textarea id="jRest" rows="4" style="width:100%">${esc(split.rest)}</textarea>
    <p><button id="jSave">메모 저장</button> <button id="jPlan">마감 초안</button>
       <span id="jSaveState"></span> <span class="muted" id="jMsg"></span></p>
    <div id="jClose"></div>
    <div id="jNew" class="panel" style="margin-top:.8rem"></div><div id="jOperations"></div>`;

  // 입력 즉시 초안으로 잡아 둔다 — 다시 그려도(탭·프로젝트 전환) 타이핑이 살아남는다(UX-01).
  if(v.complete===false || v.warnings?.length)$('#jBody').insertAdjacentHTML('afterbegin',`<p id="${v.complete===false?'jIncomplete':'jScopeNotice'}" class="warn">${(v.warnings||['Jira 조회 불완전 — 일부 결과입니다.']).map(esc).join('<br>')}</p>`);
  $("#jBody").querySelectorAll("textarea").forEach(t => t.oninput = () => {
    J.draft = collectMemo(J, t); J.dirty = true; J.editRevision++; J.plan = null;
    const oldPlan = $("#jClose"); if (oldPlan) oldPlan.innerHTML = '';
    renderSaveState(J);
  });
  renderSaveState(J);
  $("#jSave").onclick = () => busy($("#jSave"), () => saveMemo(p), "저장 중…");
  // 초안은 서버가 **파일**을 읽어 만든다. 저장하지 않은 입력이 있으면 먼저 저장한다 — 안 그러면
  // 옛 문장이 승인 표에 올라간다(UX-04).
  $("#jPlan").onclick = async () => { await busy($("#jPlan"), async () => { if (J.dirty && !await saveMemo(p)) return; await planClose(p); }, "초안 만드는 중…"); renderSaveState(J); };
  newTaskPanel(p);
  if (J.info.schema_ok === false) ['#jSave', '#jPlan', '#nPlan'].forEach(s => { const el = $(s); if (el) el.disabled = true; });
  if (J.plan) drawClose(p);
  drawOperations(p, J);
}

function collectMemo(J, changed) {
  const text = memoTextFor(J), edits = {};
  if (changed.dataset.key) edits[changed.dataset.key] = changed.value.split("\n").map(x => x.trim()).filter(Boolean);
  return patchMemo(text, edits, changed.dataset.key ? splitMemo(text).rest : changed.value);
}

// 저장 성공이면 true. 화면에 없는 프로젝트(guardUnsaved 경유)도 draft 로 저장할 수 있다.
async function saveMemo(p, J = jiraDoc(p)) {
  if (!J.dirty && !J.pendingMemo) return true;
  if (!J.memo) return false;
  const text = J.draft, revision = J.editRevision, memo = J.memo, ymd = J.ymd;
  try {
    const base = `/api/v1/projects/${p.id}/writes/memo`;
    let saved, submitted;
    if (J.pendingMemo) {
      submitted = J.pendingMemo;
      const checked = await api('POST', `${base}/operations/${encodeURIComponent(submitted.operation_id)}`, {});
      if (checked.status === 'failed') J.pendingMemo = null;
      if (checked.status !== 'succeeded') throw new Error(`저장 결과 ${checked.status} · ${submitted.operation_id}. 재전송하지 않고 결과를 확인하십시오.`);
      saved = checked.result;
    } else {
      const plan = await api('POST', `${base}/plan`, {action:'save',payload:{ymd,text,sha:memo.sha,document_id:memo.document_id}});
      if (J.editRevision !== revision || J.draft !== text) return false;
      submitted = J.pendingMemo = {...plan, text, revision};
      saved = await api('POST', `${base}/apply`, {plan_id:plan.plan_id,operation_id:plan.operation_id});
      if (saved.status !== 'succeeded') {
        if (saved.status === 'failed') J.pendingMemo = null;
        throw new Error(`저장 결과 ${saved.status} · ${plan.operation_id}`);
      }
    }
    if (saved.document_id !== memo.document_id || saved.ymd !== ymd) throw new Error('저장 응답의 문서가 다릅니다');
    J.memo = { ...memo, ...saved };
    J.pendingMemo = null;
    if (J.editRevision === submitted.revision && J.draft === submitted.text) { J.dirty = false; J.draft = null; }
    J.savedAt = new Date().toTimeString().slice(0, 5);
    renderSaveState(J);
    const m = jiraVisible(p, J) && $("#jMsg"); if (m) m.textContent = "";
    return !J.dirty;
  } catch (e) {
    if (jiraVisible(p, J) && e.data && e.data.current_text !== undefined) {
      J.pendingMemo = null;
      // 덮지 않았다. 사람이 보고 고른다.
      const m = $("#jMsg"); if (m) m.innerHTML = '<span class="warn">메모가 다른 곳에서 바뀌어 저장하지 않았습니다.</span>';
      const c = $("#jClose"); if (c) {
        c.innerHTML = `<p class="warn">현재 파일의 메모입니다. 필요한 내용을 위 칸에 옮긴 뒤 다시 저장하십시오.</p>
          <textarea rows="6" style="width:100%" readonly>${esc(e.data.current_text)}</textarea>
          <p><button id="jReload">파일 내용으로 다시 열기 (입력한 내용은 버립니다)</button></p>`;
        $("#jReload").onclick = () => { J.dirty = false; J.draft = null; loadJira(p); };
      }
      return false;
    }
    renderSaveState(J); fail(e); return false;
  }
}

async function planClose(p) {
  const J = jiraDoc(p), revision = J.editRevision;
  try {
    const plan = await api("POST", `/api/projects/${p.id}/jira/close/plan`, { ymd: J.ymd });
    if (revision !== J.editRevision || J.dirty || plan.memo_sha !== J.memo.sha) return;
    J.plan = plan;
    if (jiraVisible(p, J)) drawClose(p);
  } catch (e) { fail(e); }
}

function drawClose(p) {
  const J = jiraDoc(p), pl = J.plan;
  const row = (it, i) => `<tr>
      <td><input type="checkbox" class="jPick" data-i="${i}" aria-label="${esc(it.target)} 승인"></td>
      <td><code>${esc(it.target)}</code></td>
      <td>${esc(it.payload.text)}</td>
      <td>${(it.lint || []).map(l => `<div class="warn">${esc(l.hint)}</div>`).join("") || '<span class="muted">—</span>'}</td></tr>`;
  $("#jClose").innerHTML = `
    <h3>마감 초안 — 체크해서 승인한 것만 올라갑니다</h3>
    ${pl.items.length ? `<table><tr><th style="width:2rem"></th><th>대상</th><th>코멘트</th><th>검사</th></tr>
      ${pl.items.map((it, i) => row(it, i)).join("")}</table>
      ${pl.items.some(it => it.lint.length) ? '<p><label><input type="checkbox" id="jAckLint">선택 항목의 검사 경고를 확인했습니다</label></p>' : ''}
      <p><button id="jPickAll">전체 선택</button> <button id="jApply" class="danger" disabled>승인하고 올리기</button>
         <span class="muted">Jira 에 코멘트를 씁니다 — 되돌릴 수 없습니다. 완료(Done) 전이는 하지 않습니다.</span></p>`
      : '<p class="muted">올릴 코멘트가 없습니다.</p>'}
    ${pl.unknown_keys.length ? `<p class="warn">원본에 없는 키: ${pl.unknown_keys.map(i => esc(i.target)).join(", ")} — 오타이거나 이 폴더 범위 밖입니다. 올리지 않습니다.</p>` : ""}
    ${pl.deferred.length ? `<p class="warn">티켓 후보 ${pl.deferred.length}건 (${pl.deferred.map(d => esc(d.mark)).join(" ")}) — 아래 「새 태스크 · 파생」에서 만들 수 있습니다. 메모에는 그대로 남아 있습니다.</p>` : ""}
    ${pl.pending.length ? `<details class="warn"><summary>승인됐는데 아직 안 올라간 항목 ${pl.pending.length}건</summary>
      <table>${pl.pending.map(x => `<tr><td><code>${esc(x.target)}</code></td><td>${esc(x.kind)}</td><td class="muted">${esc(x.approved_at || "")}</td></tr>`).join("")}</table>
      <p class="muted">결과 미확인 항목은 새 승인으로 재전송하지 않습니다. 아래 작업 상태에서 원격 ID로 대조하십시오.</p></details>` : ""}
    <div id="jResult"></div>`;
  const picks = () => [...document.querySelectorAll(".jPick")];
  const sync = () => { const b = $("#jApply"), selected = picks().filter(x => x.checked); if (b) b.disabled = !selected.length || (selected.some(x => pl.items[+x.dataset.i].lint.length) && !$("#jAckLint").checked); };
  picks().forEach(x => x.onchange = sync);
  const all = $("#jPickAll"); if (all) all.onclick = () => { picks().forEach(x => x.checked = true); sync(); };
  const btn = $("#jApply");
  const ack = $("#jAckLint"); if (ack) ack.onchange = sync;
  if (btn) btn.onclick = () => applyClose(p, picks().filter(x => x.checked).map(x => pl.items[+x.dataset.i]), btn, pl, !!(ack && ack.checked));
}

// 올린 결과는 toast 가 아니라 표로 남긴다 — 무엇이 올라갔고 무엇이 실패했는지, 실패는 바로 다시 시도(UX-18).
async function applyClose(p, items, btn, plan, ackLint = false) {
  const J = jiraDoc(p);
  if (!items.length) return toast("올릴 항목을 고르십시오");
  await busy(btn, async () => {
    try {
      const r = await api("POST", `/api/projects/${p.id}/jira/close/apply`, { ymd: J.ymd, plan_id: plan.plan_id,
        operation_ids: items.map(it => it.operation_id), ack_lint: ackLint });
      if (!jiraVisible(p, J)) return;
      const failed = new Map(r.failed.map(f => [f.target, f]));
      const res = $("#jResult") || $("#jClose");
      res.innerHTML = `<h3>올린 결과 — 성공 ${r.done.length} · 실패 ${r.failed.length}</h3>
        <table><tr><th>대상</th><th>결과</th></tr>
        ${items.map(it => failed.has(it.target)
          ? `<tr><td><code>${esc(it.target)}</code></td><td class="bad">${esc(failed.get(it.target).status)} — ${esc(failed.get(it.target).error)}</td></tr>`
          : `<tr><td><code>${esc(it.target)}</code></td><td class="ok">올림</td></tr>`).join("")}</table>
        ${r.failed.some(f => f.retry_allowed) ? '<p><button id="jRetry" class="danger">명시적으로 거절된 항목만 다시 시도</button></p>' : ""}
        ${r.failed.some(f => !f.retry_allowed) ? '<p class="warn">결과 미확인 항목은 재전송하지 않습니다. 작업 상태에서 원격 결과를 대조하십시오.</p>' : ''}`;
      const retry = $("#jRetry");
      const retryItems = items.filter(it => failed.has(it.target) && failed.get(it.target).retry_allowed);
      if (retry) retry.onclick = () => applyClose(p, retryItems, retry, plan, ackLint);
      toast(`올림 ${r.done.length}건${r.failed.length ? ` · 실패 ${r.failed.length}건 (아래 표 참조)` : ""}`);
      // 승인 표는 치우되 결과 표는 남긴다. 태스크 표는 다시 읽는다(안 올라간 파생·배지 갱신).
      J.plan = null;
      const keep = res.innerHTML;
      await loadJira(p);
      if (!jiraVisible(p, J)) return;
      const slot = $("#jClose"); if (slot) slot.innerHTML = keep;
      const rt = $("#jRetry"); if (rt) rt.onclick = () => applyClose(p, retryItems, rt, plan, ackLint);
    } catch (e) { fail(e); }
  }, "올리는 중…");
}

async function drawOperations(p, J) {
  try {
    const response = await api('GET', `/api/projects/${p.id}/jira/operations`);
    if (!jiraVisible(p, J)) return;
    const box = $('#jOperations'); if (!box) return;
    const pending = response.operations.filter(op => ['executing', 'uncertain', 'failed'].includes(op.status) || (op.status === 'succeeded' && (!op.receipt_exported || (op.kind === 'create_issue' && !op.local_recorded))));
    box.innerHTML = pending.length ? `<h3>확인이 필요한 작업</h3>${pending.map((op, i) => `<div class="panel"><p><code>${esc(op.target)}</code> · ${esc(op.status)} <small>${esc(op.operation_id)}</small></p>
      ${['executing', 'uncertain'].includes(op.status) ? `<label>Jira에서 확인한 원격 ${op.kind === 'comment' ? '코멘트 ID' : '티켓 키'} <input data-remote="${i}"></label><button data-reconcile="${i}">원격 결과 대조</button>` : op.status === 'succeeded' ? `<button data-repair="${i}">성공한 작업의 로컬 기록 복구</button>` : '<p class="muted">다시 미리보기하여 명시적으로 거절된 작업을 승인하십시오.</p>'}</div>`).join('')}` : '';
    box.querySelectorAll('[data-repair]').forEach(button => button.onclick = () => busy(button, async () => {
      try { await api('POST', `/api/projects/${p.id}/jira/operations/${encodeURIComponent(pending[+button.dataset.repair].operation_id)}/repair`, {}); toast('로컬 기록을 복구했습니다'); drawOperations(p, J); }
      catch (e) { fail(e); }
    }, '복구 중…'));
    box.querySelectorAll('[data-reconcile]').forEach(button => button.onclick = () => busy(button, async () => {
      const i = +button.dataset.reconcile, remoteId = box.querySelector(`[data-remote="${i}"]`).value.trim();
      try { await api('POST', `/api/projects/${p.id}/jira/operations/${encodeURIComponent(pending[i].operation_id)}/reconcile`, { remote_id: remoteId }); toast('원격 결과를 확인했습니다'); drawOperations(p, J); }
      catch (e) { fail(e); }
    }, '대조 중…'));
    if (J.info.schema_ok === false) box.querySelectorAll('button').forEach(button => button.disabled = true);
    const local=(response.common_writes||[]).filter(op=>op.status!=='planned');
    if(local.length)box.insertAdjacentHTML('beforeend',`<h3>로컬 메모 작업</h3>${local.map((op,i)=>`<p>${esc(op.ymd)} · ${esc(op.status)} <code>${esc(op.operation_id)}</code> <button data-memo-check="${i}">저장 결과 확인</button></p>`).join('')}`);
    box.querySelectorAll('[data-memo-check]').forEach(button=>button.onclick=()=>busy(button,async()=>{
      const op=local[+button.dataset.memoCheck];
      if(op.ymd===J.ymd){if(!J.pendingMemo)J.pendingMemo=op;await saveMemo(p,J);}
      else{try{const r=await api('POST',`/api/v1/projects/${p.id}/writes/memo/operations/${encodeURIComponent(op.operation_id)}`,{});toast(`${op.ymd}: ${r.status}`);}catch(e){fail(e);}}
      if(jiraVisible(p,J))drawOperations(p,J);
    },'확인 중…'));
    const legacy = response.legacy_pending.filter(op => op.status === 'legacy_uncertain');
    if (legacy.length) box.insertAdjacentHTML('beforeend', `<h3>구형 승인 — 수동 확인 필요</h3><p class="warn">이 로그만으로 과거 전송 여부를 확정할 수 없습니다. Jira 기록과 원래 승인 내역을 직접 확인해야 합니다. 자동 재전송과 작업 UUID 대조를 제공하지 않습니다.</p><ul>${legacy.map(op => `<li><code>${esc(op.target)}</code> · ${esc(op.kind)} · ${esc(op.id)}</li>`).join('')}</ul>`);
  } catch (e) { if (jiraVisible(p, J)) { const box = $('#jOperations'); if (box) box.textContent = '작업 상태를 확인하지 못했습니다: ' + e.message; } }
}

// ------------------------------------------------------------- 설정 작성 (S5b)
// 사이트 공통값은 다시 묻지 않는다 — 같은 값을 두 번 입력받으면 언젠가 어긋나고,
// 어긋난 쪽이 조용히 낡는다. 폴더마다 다른 것만 묻는다.
async function initPanel(p, info) {
  const box = $("#jInitBox"), J = jiraFor(jiraState, p.id);   // 설정 초안도 프로젝트마다 (UX-02)
  const sc = info.scope || {};
  J.cfg = J.cfg || {
    project_key: sc.project_key || "", csc_field: sc.csc_field || "", csc_value: sc.csc_value || "",
    epic_keys: (sc.epic_keys || []).join(","), title_include: (sc.title_include || []).join(","),
    title_exclude: (sc.title_exclude || []).join(","), title_prefix: "",
    site_url: info.site_url || "",
  };
  const c = J.cfg;
  let donors = [];
  try { donors = (await api("GET", `/api/projects/${p.id}/jira/init/donors`)).donors; } catch (e) { /* 없어도 된다 */ }
  const gi = await api("GET", `/api/projects/${p.id}/jira/gitignore`);

  box.innerHTML = `
    <div class="panel" style="margin-top:.6rem">
      <h3>${info.enabled ? "설정 수정" : "설정 작성"} <span class="muted">watermark 와 roots 는 건드리지 않습니다</span></h3>
      ${donors.length ? `<p><label>사이트 공통값 가져오기
        <select id="iDonor"><option value="">— 고르지 않음 —</option>
        ${donors.map((d, i) => `<option value="${i}">${esc(d.project_key || "?")} · ${esc(d.site_url)}</option>`).join("")}</select></label>
        <span class="muted">site_url · csc_field · issue_type_map 을 그대로 씁니다</span></p>` : ""}
      <p><label>사이트 URL <input id="iSite" size="40" value="${esc(c.site_url)}"></label>
         <label>프로젝트 키 <input id="iKey" size="8" value="${esc(c.project_key)}"></label>
         <button id="iDisc" title="Jira 에서 이슈 타입·CSC 필드 후보·에픽 목록을 읽어 옵니다 (읽기 전용)">Jira 에서 후보 가져오기</button></p>
      <div id="iDiscBox"></div>
      <p><label>CSC 필드 <input id="iCscField" size="22" value="${esc(c.csc_field)}"></label>
         <label>CSC 값 <input id="iCscValue" size="28" value="${esc(c.csc_value)}"></label></p>
      <p><label>에픽(CSU) 키 <input id="iEpics" size="40" value="${esc(c.epic_keys)}" placeholder="쉼표로 구분 · 비우면 CSC 전체"></label></p>
      <p><label>제목 포함 <input id="iInc" size="20" value="${esc(c.title_include)}"></label>
         <label>제외 <input id="iExc" size="20" value="${esc(c.title_exclude)}"></label>
         <label>접두사 <input id="iPrefix" size="10" value="${esc(c.title_prefix)}"></label>
         <span class="muted">정규식 · 쉼표로 구분. JQL 에는 들어가지 않습니다</span></p>
      ${gi.applicable && gi.missing.length ? `<p class="warn">이 저장소의 .gitignore 에 ${gi.missing.map(esc).join(" · ")} 가 없습니다 — 그대로 두면 업무 기록이 커밋됩니다.
         <label><input type="checkbox" id="iFixGi" checked> 적용할 때 함께 추가</label></p>` : ""}
      <p><button id="iPrev" class="primary">미리보기</button> <button id="iApply" disabled title="미리보기로 바뀌는 항목을 확인한 뒤 적용할 수 있습니다">적용</button>
         <span class="muted" id="iMsg">미리보기 → 적용 순서입니다. watermark·roots 는 보존됩니다.</span></p>
      <div id="iPrevBox"></div>
    </div>`;

  const d = $("#iDonor");
  if (d) d.onchange = () => {
    const pick = donors[+d.value];
    if (!pick) return;
    $("#iSite").value = pick.site_url;
    if (pick.defaults.csc_field) $("#iCscField").value = pick.defaults.csc_field;
    J.cfg.issue_type_map = pick.defaults.issue_type_map;
    $("#iMsg").textContent = "사이트 공통값을 가져왔습니다";
  };
  $("#iDisc").onclick = () => busy($("#iDisc"), () => discover(p), "가져오는 중…");
  $("#iPrev").onclick = () => busy($("#iPrev"), () => initSubmit(p, "preview"));
  $("#iApply").onclick = () => busy($("#iApply"), () => initSubmit(p, "apply"), "저장 중…");
  // 값이 바뀌면 미리보기가 낡는다 — 다시 보기 전에는 적용을 막는다(배포와 같은 규칙, UX-37).
  box.querySelectorAll("input, select").forEach(el => el.addEventListener("input", () => { const a = $("#iApply"); if (a) a.disabled = true; $("#iPrevBox").innerHTML = ""; }));
}

function initCfg(J) {
  const list = v => v.split(",").map(x => x.trim()).filter(Boolean);
  const cfg = {
    project_key: $("#iKey").value.trim(),
    csc_field: $("#iCscField").value.trim() || null,
    csc_value: $("#iCscValue").value.trim() || null,
    epic_keys: list($("#iEpics").value),
    title_include: list($("#iInc").value),
    title_exclude: list($("#iExc").value),
    title_prefix: $("#iPrefix").value.trim() || null,
    issue_type_map: Object.assign({}, J.cfg.issue_type_map || {}, { site_url: $("#iSite").value.trim() }),
  };
  return cfg;
}

async function discover(p) {
  const box = $("#iDiscBox"), J = jiraFor(jiraState, p.id);
  box.innerHTML = '<p class="muted">조회 중…</p>';
  try {
    const r = J.disc = await api("POST", `/api/projects/${p.id}/jira/init/discover`,
      { site: $("#iSite").value.trim(), project_key: $("#iKey").value.trim() || null });
    const errs = r.errors ? Object.entries(r.errors).map(([k, v]) => `<div class="warn">${esc(k)}: ${esc(v)}</div>`).join("") : "";
    box.innerHTML = errs + `
      ${(r.csc_candidates || []).length ? `<p>CSC 후보 ${(r.csc_candidates || []).map((x, i) =>
        `<button data-csc="${i}">${esc(x.field)} · ${esc(x.name || "")}</button>`).join(" ")}</p>` : ""}
      ${(r.epics || []).length ? `<p>에픽 ${r.epics.length}개
        <button id="iAllEpics">전부 넣기</button>
        <span class="muted">${r.epics.slice(0, 6).map(e => esc(e.key)).join(" ")}${r.epics.length > 6 ? " …" : ""}</span></p>` : ""}`;
    box.querySelectorAll("[data-csc]").forEach(b => b.onclick = () => {
      const x = r.csc_candidates[+b.dataset.csc];
      $("#iCscField").value = x.field;
      toast(x.why);
    });
    const ae = $("#iAllEpics");
    if (ae) ae.onclick = () => { $("#iEpics").value = r.epics.map(e => e.key).join(","); };
  } catch (e) {
    box.innerHTML = `<p class="warn">${esc(e.message)}</p><p class="muted">조회는 편의일 뿐입니다 — 값을 직접 넣어도 됩니다.</p>`;
  }
}

async function initSubmit(p, which) {
  const J = jiraFor(jiraState, p.id);
  if (which === "apply" && !await guardJiraUnsaved(p)) return;   // 적용하면 화면을 새로 그린다
  try {
    const r = await api("POST", `/api/projects/${p.id}/jira/init/${which}`,
      { cfg: initCfg(J), fix_gitignore: !!($("#iFixGi") && $("#iFixGi").checked) });
    if (which === "preview") {
      const ch = Object.entries(r.preview.changed);
      $("#iPrevBox").innerHTML = `
        <table><tr><th>항목</th><th>현재</th><th>바뀔 값</th></tr>
        ${ch.length ? ch.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(JSON.stringify(v.before))}</td><td>${esc(JSON.stringify(v.after))}</td></tr>`).join("")
          : '<tr><td colspan="3" class="muted">바뀌는 항목이 없습니다</td></tr>'}</table>
        <p class="muted">보존: watermark ${esc(r.preview.preserved.watermark)} · roots ${r.preview.preserved.roots}개</p>`;
      const a = $("#iApply"); if (a) { a.disabled = false; a.className = ch.length ? "primary" : ""; }
      $("#iMsg").textContent = ch.length ? "위 변경을 확인했으면 적용하십시오." : "바뀌는 항목이 없습니다.";
      return;
    }
    toast("설정을 저장했습니다");
    J.cfg = null; J.view = null; J.memo = null; J.plan = null; J.dirty = false; J.draft = null;
    for (const doc of J.documents.values()) { doc.requestRevision++; doc.view = null; doc.plan = null; if (!doc.dirty) doc.memo = null; }
    renderProject();
  } catch (e) {
    if (e.data && e.data.gitignore) {
      $("#iMsg").innerHTML = '<span class="warn">.gitignore 추가에 동의하거나 직접 넣은 뒤 다시 시도하십시오 — 저장하지 않았습니다.</span>';
      return;
    }
    fail(e);
  }
}

// ------------------------------------------------------------- 새 태스크 (S6)
async function newTaskPanel(p) {
  const J = jiraDoc(p), v = J.view, sc = (J.info.scope || {});
  const epics = [...new Set(v.tasks.map(t => t.parent_key).filter(Boolean).concat(sc.epic_keys || []))];
  $("#jNew").innerHTML = `
    <h3>새 태스크 · 파생</h3>
    <p><label>제목 <input id="nSum" size="46"></label>
       <label>종류 <select id="nKind"><option value="task">메인 태스크</option><option value="subtask">파생</option><option value="bug">문제(BUG)</option></select></label></p>
    <p><label>부모 에픽 <select id="nEpic"><option value="">${(sc.epic_keys || []).length === 1 ? esc(sc.epic_keys[0]) + " (자동)" : "— 고르지 않음 —"}</option>
        ${epics.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join("")}</select></label>
       <label>부모 태스크 <select id="nParent"><option value="">— 파생일 때만 —</option>
        ${v.tasks.map(t => `<option value="${esc(t.key)}">${esc(t.key)} · ${esc(t.summary.slice(0, 30))}</option>`).join("")}</select></label>
       <button id="nPlan">확인</button></p>
    <div id="nBox"><p class="muted">확인을 누르면 제목이 이 폴더 범위에 들어오는지, CSC 가 붙는지 먼저 검사합니다. 아직 만들지 않습니다.</p></div>`;
  $("#nPlan").onclick = () => planNew(p);
  ['#nSum', '#nKind', '#nEpic', '#nParent'].forEach(s => $(s).oninput = () => { J.newRevision = (J.newRevision || 0) + 1; const box = $('#nBox'); if (box) box.textContent = '내용이 바뀌었습니다 — 다시 확인하십시오.'; });
}

async function planNew(p) {
  const box = $("#nBox"), epic = $("#nEpic").value || null, J = jiraDoc(p);
  const revision = J.newRevision = (J.newRevision || 0) + 1;
  const sibs = (J.view.tasks || []).filter(t => !epic || t.parent_key === epic).map(t => t.summary);
  try {
    const it = await api("POST", `/api/projects/${p.id}/jira/new/plan`, {
      summary: $("#nSum").value, kind: $("#nKind").value,
      epic_key: epic, parent_key: $("#nParent").value || null, epic_summaries: sibs,
    });
    if (!jiraVisible(p, J) || revision !== J.newRevision) return;
    box.innerHTML = `
      <p>확정된 제목 <b>${esc(it.title)}</b></p>
      ${(it.warnings || []).map(w => `<p class="warn">${esc(w)}</p>`).join("")}
      <pre style="white-space:pre-wrap;font-size:.8rem">${esc(JSON.stringify(it.payload.fields, null, 1))}</pre>
      <p><button id="nGo" class="danger" ${it.ready_to_create===false?'disabled':''}>승인하고 만들기</button> <span class="muted">Jira 에 티켓을 만듭니다 — 되돌릴 수 없습니다. 만든 뒤 완료로 옮기지 않습니다.</span></p>`;
    $("#nGo").onclick = async () => {
      $("#nGo").disabled = true;
      try {
        const r = await api("POST", `/api/projects/${p.id}/jira/new/create`, { plan_id: it.plan_id, operation_id: it.operation_id });
        toast(`만들었습니다: ${r.key}`);
        if (jiraVisible(p, J)) {await loadJira(p);if(jiraVisible(p,J)&&r.visibility&&r.visibility.state!=='visible')$('#jNew').innerHTML=`<p class="warn">${esc(r.key)} 생성 성공 · 가시성 ${esc(r.visibility.state)}<br>${(r.visibility.warnings||[]).map(esc).join('<br>')}</p>`;}
      } catch (e) { fail(e); if (jiraVisible(p, J)) { const go = $("#nGo"); if (go) go.disabled = !!(e.data && e.data.retry_allowed === false); drawOperations(p, J); } }
    };
  } catch (e) {
    // 만들기 전에 멈춘다 — 만든 뒤에는 되돌리기가 가장 비싸다.
    if (!jiraVisible(p, J) || revision !== J.newRevision) return;
    box.innerHTML = `<p class="warn">${esc(e.message)}</p>`;
  }
}

function workflowDashboard(data) {
  return `<section id="workflowDashboard" class="box"><h3>전체 업무 상태</h3><label>기록 날짜 <input type="date" id="wfDate" min="2000-01-01" max="2099-12-31" value="${esc(data?.date||calendarDateAt())}"></label> <button id="wfReload">상태 다시 읽기</button>
    <p class="muted">저장된 배치 신호와 캘린더의 같은 원본 일지 기록입니다. 이 화면을 여는 것으로 Jira를 조회하거나 watermark를 변경하지 않습니다.</p><div id="wfRows">${workflowRows(data)}</div></section>`;
}
function workflowRows(data) {
  if(!data?.projects)return '<p class="warn">업무 상태 미확인</p>';
  return data.projects.map(p=>`<div data-workflow-project="${esc(p.id)}" style="border-top:1px solid #ddd;padding:.5rem 0;overflow-wrap:anywhere"><b>${esc(p.alias)}</b> · ${p.enabled?(p.complete?'당일 완전 배치 확인':'최신 완전 배치 미확인'):'Jira 미연결'}
    ${p.enabled?`<p>신규 ${esc(p.signals?.new??'?')} · 이어서 ${esc(p.signals?.continuing??'?')} · 정체 ${esc(p.signals?.stalled??'?')} · 미등재 CSU ${esc(p.signals?.epic_unlisted??'?')} · 제목 제외 ${esc(p.signals?.title_filtered??'?')} · CSC 미지정 ${esc(p.signals?.csc_missing??'?')} · 미등록 파생 ${esc(p.pending_children??'?')}</p>
      <p>최근 배치 ${esc(p.batch?.latest?.status||'없음')} · watermark ${esc(p.batch?.watermark||'미확인')} · 마감 누락 ${esc(({suspected:'의심',unknown:'미확인',no_record:'이전 날짜 기록 없음'})[p.missing_close]||'미확인')}</p>`:''}
    <p>수행 업무 ${esc(p.date_records.task_count??'?')} · 메모 ${esc(p.date_records.memo_count??'?')} <button data-workflow-calendar="${esc(p.id)}" data-date="${esc(data.date)}">캘린더 상세</button>${p.enabled?` <button data-workflow-jira="${esc(p.id)}" data-date="${esc(data.date)}">일지에서 보기</button>`:''}</p>
    ${(p.warnings||[]).map(w=>`<p class="warn">${esc(w)}</p>`).join('')}</div>`).join('')||'<p class="muted">등록 프로젝트가 없습니다.</p>';
}
function bindWorkflow(container) {
  const refresh=async()=>{const date=$('#wfDate').value,box=$('#wfRows'),generation=state.workflowGeneration=(state.workflowGeneration||0)+1;
    box.textContent='상태 읽는 중…';try{const r=await api('GET','/api/v1/workflow/status?date='+encodeURIComponent(date));if(!box.isConnected||generation!==state.workflowGeneration)return;state.workflow=r;box.innerHTML=workflowRows(r);links();renderList();}
    catch(e){if(box.isConnected&&generation===state.workflowGeneration)box.textContent=e.message;}};
  const links=()=>{
    container.querySelectorAll('[data-workflow-calendar]').forEach(b=>b.onclick=()=>{CAL.selected=b.dataset.date;CAL.projectId=b.dataset.workflowCalendar;renderCalendar();});
    container.querySelectorAll('[data-workflow-jira]').forEach(b=>b.onclick=()=>{jiraFor(jiraState,b.dataset.workflowJira).ymd=b.dataset.date.replaceAll('-','').slice(2);select(b.dataset.workflowJira,'jira');});
  };
  $('#wfDate').onchange=refresh;$('#wfReload').onclick=refresh;links();
}
function workCalendarBadges(p,chip) {
  const jira=state.jira[p.id];
  if(jira?.enabled)chip(jira.schema_ok===false||jira.gitignore_ok===false?'warn':'ok','Jira'+(jira.schema_ok===false?' schema!':jira.gitignore_ok===false?' .gitignore!':''),'jira',jira.schema_ok===false?jira.reason:jira.gitignore_ok===false?'업무 데이터가 .gitignore 로 제외되지 않았습니다':`Jira 연동 켜짐 · ${(jira.scope||{}).project_key||''}`);
  const flow=state.workflow?.projects?.find(item=>item.id===p.id);
  if(flow?.enabled)chip(flow.complete?'':'warn',flow.complete?'일일 집계 확인':'업무 상태 확인 필요','jira',flow.warnings.join(' · '));
}

window.wsflowUI.register('work-calendar',{
  tabs:[{id:'jira',label:'Jira',order:50,render:renderJira}],
  calendar:renderCalendar,guardUnsaved:guardJiraUnsaved,clearProject:id=>jiraState.delete(id),
  workflowDashboard,bindWorkflow,badges:workCalendarBadges,
  async refreshProjects(projects){await Promise.all(projects.map(async project=>{try{state.jira[project.id]=await api('GET',`/api/projects/${project.id}/jira`);}catch(error){state.jira[project.id]=null;}}));}
});
