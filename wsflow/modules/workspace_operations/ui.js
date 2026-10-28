// Workspace views; loaded only for the active trusted package.
function fileRows(list, name) {
  if (!list.length) return '<span class="muted">없음</span>';
  return `<div class="filelist">${list.map(f => { const path = f.path ?? f, code = f.code ?? "?"; return `<label><input type="checkbox" name="${name}" value="${esc(path)}"> <span class="code">${esc(code)}</span>${esc(path)}</label>`; }).join("")}</div>`;
}

async function renderStatus(body, p) {
  const [st, rem] = await Promise.all([api("GET", `/api/projects/${p.id}/status`), api("GET", `/api/projects/${p.id}/remotes`)]);
  if(!body.isConnected||state.current!==p.id||state.tab!=='status')return;
  const remotes = rem.remotes;
  body.innerHTML = `
    <div class="box"><h3>브랜치</h3>
      <b>${esc(st.branch || "(detached)")}</b> ${st.upstream ? `→ ${esc(st.upstream)} <span class="chip ${st.ahead ? "ok" : ""}">↑${st.ahead}</span><span class="chip ${st.behind ? "warn" : ""}">↓${st.behind}</span>` : '<span class="chip warn">upstream 없음</span>'}
      ${st.stashes ? `<span class="chip">stash ${st.stashes}</span>` : ""} ${st.clean ? '<span class="chip ok">clean</span>' : ""}
      ${st.conflicted.length ? `<div class="bad">충돌: ${st.conflicted.map(esc).join(", ")}</div>` : ""}
      <p id="gitFetchTime" class="muted">마지막 성공 fetch: ${esc(st.sync?.last_fetch_at||'미확인')} · ↑/↓는 마지막 fetch의 로컬 추적 ref 기준입니다.</p>
      <div class="actions"><button id="btnFetch">fetch</button><button id="btnPull">ff-only pull</button><span class="muted">변경 파일·분기·충돌은 직접 확인합니다.</span></div>
    </div>
    <div class="grid2">
      <div class="box"><h3>스테이징됨 (${st.staged.length})</h3>${fileRows(st.staged, "staged")}</div>
      <div class="box"><h3>변경 / 미추적 (${st.unstaged.length + st.untracked.length})</h3>${fileRows([...st.unstaged, ...st.untracked.map(u => ({ code: "?", path: u }))], "unstaged")}
        <div class="actions"><button id="btnAddSel">선택 스테이징</button><button id="btnAddAll">전부 스테이징 (add -A)</button></div></div>
    </div>
    <div class="box"><h3>커밋</h3>
      <textarea id="msg" placeholder="커밋 메시지 (한글 그대로 — UTF-8 파일로 전달됩니다)"></textarea>
      <div class="actions"><label><input type="checkbox" id="addAll"> 커밋 전 add -A</label><button class="primary" id="btnCommit">커밋</button></div></div>
    <div class="box"><h3>push</h3>
      <div class="actions">
        <select id="pushRemote">${remotes.map(r => `<option>${esc(r.name)}</option>`).join("") || "<option value=''>(remote 없음)</option>"}</select>
        <input type="text" id="pushBranch" value="${esc(st.branch || "")}" placeholder="브랜치">
        <label><input type="checkbox" id="setUp" ${st.upstream ? "" : "checked"}> -u (upstream 설정)</label>
        <button class="danger" id="btnPush" ${remotes.length ? "" : "disabled"} title="원격에 씁니다 — 확인 창이 뜹니다">push</button>
        ${remotes.length ? "" : '<span class="muted">remote 탭에서 먼저 remote 를 추가하세요.</span>'}
      </div></div>
    <div id="gitOperations"></div><pre id="out" class="muted" hidden></pre>`;
  const out = r => { const o = $("#out"); o.hidden = false; o.textContent = typeof r === "string" ? r : [r.command, r.stdout, r.stderr].filter(Boolean).join("\n"); };
  for(const [id,action] of [['btnFetch','fetch'],['btnPull','pull']])$('#'+id).onclick=async()=>{
    const button=$('#'+id);await busy(button,async()=>{try{const r=await gitWrite(p,action,{});
      if(!body.isConnected||state.current!==p.id)return;
      const message=[r.ok?'성공':'실행 중단: '+({dirty:'미저장/미추적 파일 있음',non_ff:'분기됨 · 자동 병합하지 않음',conflict:'충돌 확인 필요',detached:'detached HEAD',no_upstream:'upstream 없음',no_remote:'remote 없음',fetch_failed:'fetch 실패',pull_failed:'ff-only pull 실패'}[r.reason]||r.reason||'실패'),r.command,r.stdout,r.stderr].filter(Boolean).join('\n');
      await loadAll();
      if(!body.isConnected||state.current!==p.id)return;
      await renderStatus(body,cur()||p);out(message);
    }catch(e){if(body.isConnected)out(e.message);}},action+' 중…');};
  drawGitOperations(body,p);
  const checked = n => [...body.querySelectorAll(`input[name=${n}]:checked`)].map(i => i.value);
  $("#btnAddSel").onclick = async () => { const paths = checked("unstaged"); if (!paths.length) return toast("선택된 파일이 없습니다"); try { await gitWrite(p,"add",{paths}); await refreshCurrent(); } catch (e) { fail(e); } };
  $("#btnAddAll").onclick = async () => { try { await gitWrite(p,"add",{all:true}); await refreshCurrent(); } catch (e) { fail(e); } };
  $("#btnCommit").onclick = async () => {
    const message = $("#msg").value; if (!message.trim()) return toast("커밋 메시지를 입력하세요");
    try { const r = await gitWrite(p,"commit",{message,add_all:$("#addAll").checked}); toast("커밋 완료 " + r.hash.slice(0, 8)); await refreshCurrent(); } catch (e) { fail(e); }
  };
  $("#btnPush").onclick = async () => {
    const remote = $("#pushRemote").value, branch = $("#pushBranch").value.trim(), set_upstream = $("#setUp").checked;
    if (!await confirmDanger("원격에 push", [["명령", `<code>git push ${set_upstream ? "-u " : ""}${esc(remote)} ${esc(branch)}</code>`], ["대상", `<code>${esc((remotes.find(r => r.name === remote) || {}).fetch || remote)}</code>`], ["되돌리기", "push 된 커밋은 원격에 남습니다"]], "push")) return;
    $("#btnPush").disabled = true; out("push 중…");
    try { const r = await gitWrite(p,"push",{remote,branch,set_upstream}); out(r); toast(r.ok ? "push 성공" : "push 실패 (아래 출력 확인)"); if (r.ok) await loadAll(); }
    catch (e) { fail(e); out(e.data || e.message); } finally { const b = $("#btnPush"); if (b) b.disabled = false; }
  };
}

const gitPending = new Map();
async function gitWrite(p, action, payload) {
  const key=JSON.stringify([p.id,p.path]),base=`/api/v1/projects/${p.id}/writes/git`,requested=JSON.stringify(payload);
  let pending=gitPending.get(key);
  if(pending){
    const checked=await api('POST',`${base}/operations/${encodeURIComponent(pending.operation_id)}`,{});
    if(['succeeded','failed'].includes(checked.status))gitPending.delete(key);
    if(checked.status!=='succeeded')throw new Error(`작업 ${pending.operation_id}: ${checked.status}. 재전송 없이 결과를 확인하십시오.`);
    if(pending.action!==action||pending.requested!==requested)throw new Error('이전 작업의 완료를 확인했습니다. 새 입력은 유지했습니다. 새 작업을 다시 요청하십시오.');
    return checked.result;
  }
  const plan=await api('POST',`${base}/plan`,{action,payload});
  pending={...plan,requested};gitPending.set(key,pending);
  let result;
  try{result=await api('POST',`${base}/apply`,{plan_id:plan.plan_id,operation_id:plan.operation_id});}
  catch(e){throw new Error(`작업 ${plan.operation_id} 결과 확인 필요: ${e.message}`);}
  if(['succeeded','failed'].includes(result.status))gitPending.delete(key);
  if(result.status==='uncertain')throw new Error(`작업 ${plan.operation_id}의 결과 확인이 필요합니다. 자동 재전송하지 않습니다.`);
  return result;
}
async function drawGitOperations(body,p){
  try{
    const response=await api('GET',`/api/v1/projects/${p.id}/writes/git/operations`);
    if(!body.isConnected||state.current!==p.id)return;
    const rows=response.operations.filter(op=>op.status!=='planned').slice(-10),box=$('#gitOperations',body);
    if(!box)return;
    box.innerHTML=rows.length?`<p>최근 Git 작업</p>${rows.map((op,i)=>`<p><code>${esc(op.operation_id)}</code> ${esc(op.action)} · ${esc(op.status)} <button data-git-check="${i}">결과 확인</button></p>`).join('')}`:'';
    const active=rows.find(op=>['executing','uncertain'].includes(op.status)),key=JSON.stringify([p.id,p.path]);
    if(active&&!gitPending.has(key))gitPending.set(key,active);
    box.querySelectorAll('[data-git-check]').forEach(button=>button.onclick=()=>busy(button,async()=>{
      try{const op=rows[+button.dataset.gitCheck],r=await api('POST',`/api/v1/projects/${p.id}/writes/git/operations/${encodeURIComponent(op.operation_id)}`,{});
        if(['succeeded','failed'].includes(r.status)&&gitPending.get(key)?.operation_id===op.operation_id)gitPending.delete(key);
        if(body.isConnected){const out=$('#out',body);out.hidden=false;out.textContent=JSON.stringify(r,null,2);drawGitOperations(body,p);}
      }catch(e){fail(e);}
    },'확인 중…'));
  }catch(e){if(body.isConnected){const box=$('#gitOperations',body);if(box)box.textContent='Git 작업 조회 실패: '+e.message;}}
}

async function renderLog(body, p) {
  const n = 100;
  const r = await api("GET", `/api/projects/${p.id}/log?n=${n}`);
  if (!r.commits.length) { body.innerHTML = '<p class="muted">커밋이 없습니다.</p>'; return; }
  body.innerHTML = `<div class="tblwrap"><table><tr><th>해시</th><th>일시</th><th>작성자</th><th>제목</th><th>ref</th></tr>
    ${r.commits.map(c => `<tr class="click" data-h="${c.hash}"><td><code>${c.short}</code></td><td>${esc(c.date.replace("T", " ").slice(0, 16))}</td><td>${esc(c.author)}</td><td>${esc(c.subject)}</td><td>${c.refs.map(x => `<span class="chip">${esc(x)}</span>`).join("")}</td></tr>`).join("")}</table></div>
    <div id="commitDetail"></div>`;
  body.querySelectorAll("tr.click").forEach(tr => tr.onclick = async () => {
    try { const d = await api("GET", `/api/projects/${p.id}/commit/${tr.dataset.h}`);
      $("#commitDetail").innerHTML = `<div class="box" style="margin-top:1rem"><h3><code>${d.hash.slice(0, 12)}</code> · ${esc(d.author)} · ${esc(d.date)}</h3><pre>${esc(d.message)}</pre><b>변경 파일 (${d.files.length})</b><pre>${esc(d.files.join("\n"))}</pre></div>`;
      $("#commitDetail").scrollIntoView({ behavior: "smooth" }); } catch (e) { fail(e); }
  });
}

async function renderRemotes(body, p) {
  const r = await api("GET", `/api/projects/${p.id}/remotes`);
  body.innerHTML = `<div class="box"><h3>remote (${r.remotes.length})</h3>
    ${r.remotes.length ? `<table><tr><th>이름</th><th>fetch</th><th>push</th><th></th></tr>${r.remotes.map(x => `<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.fetch)}</td><td>${esc(x.push)}</td>
      <td><button data-edit="${esc(x.name)}" data-url="${esc(x.fetch)}">URL 변경</button> <button class="danger" data-del="${esc(x.name)}">제거</button></td></tr>`).join("")}</table>` : '<p class="warn">remote 가 없습니다 — 이 저장소는 원격 백업이 없습니다.</p>'}
    </div>
    <div class="box"><h3>remote 추가</h3><div class="actions"><input type="text" id="rName" value="origin" style="min-width:8rem"><input type="text" id="rUrl" placeholder="https://github.com/user/repo.git" style="min-width:26rem"><button class="primary" id="btnRemAdd">추가</button></div></div>`;
  $("#btnRemAdd").onclick = async () => { try { await gitWrite(p,"remote_add",{name:$("#rName").value.trim(),url:$("#rUrl").value.trim()}); await refreshCurrent(); } catch (e) { fail(e); } };
  body.querySelectorAll("[data-edit]").forEach(b => b.onclick = async () => {
    const ok = await ask({ title: `remote '${b.dataset.edit}' 의 URL 변경`,
      body: `<p class="muted">push·fetch 대상이 바뀝니다. 로컬 설정만 바뀌며 원격에는 영향이 없습니다.</p><input type="text" id="askUrl" value="${esc(b.dataset.url)}" style="width:100%">`,
      buttons: [{ label: "취소", value: false }, { label: "변경", value: true, kind: "primary" }] });
    const url = ($("#askUrl") || {}).value; if (!ok || !url || url === b.dataset.url) return;
    try { await gitWrite(p,"remote_set",{name:b.dataset.edit,url:url.trim()}); toast(`remote '${b.dataset.edit}' URL 변경`); await refreshCurrent(); } catch (e) { fail(e); } });
  body.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!await confirmDanger("remote 제거", [["remote", `<b>${esc(b.dataset.del)}</b>`], ["되돌리기", "로컬 설정만 바뀝니다 — 같은 URL 로 다시 추가하면 됩니다"]], "제거")) return;
    try { await gitWrite(p,"remote_remove",{name:b.dataset.del}); toast(`remote '${b.dataset.del}' 제거`); await refreshCurrent(); } catch (e) { fail(e); } });
}

// ------------------------------------------------------------- 규칙
const yn = (v, good = true) => v ? `<span class="${good ? "ok" : "bad"}" role="img" aria-label="있음" title="있음">●</span>` : `<span class="${good ? "bad" : "muted"}" role="img" aria-label="없음" title="없음">○</span>`;
function ledgerCell(l) {
  if (!l.exists) return '<span class="muted">없음</span>';
  const c = l.counts, inv = Object.keys(l.invalid_status || {});
  return `open <b class="${c.open ? "warn" : ""}">${c.open || 0}</b> · fixed ${c.fixed || 0} · verified <b class="ok">${c.verified || 0}</b>` + (Object.entries(c).filter(([k]) => !["open", "fixed", "verified"].includes(k)).map(([k, v]) => ` · ${k} ${v}`).join("")) + (inv.length ? ` <span class="chip bad">비허용 상태값: ${inv.map(esc).join(",")}</span>` : "");
}
function worklogCell(w) {
  if (!w.files_jsonl && !w.prompts_jsonl) return '<span class="muted">jsonl 없음</span>';
  return `${w.last_req_id ? `<b>${esc(w.last_req_id)}</b>` : "P-없음"} · prompts ${w.prompts} · files ${w.files}` + (w.uncommitted ? ` · <span class="warn">미커밋 ${w.uncommitted}</span>` : "") + (w.unverified ? ` · <span class="muted">검증 없음 ${w.unverified}</span>` : "") + (w.bad_lines ? ` · <span class="bad">파싱 실패 ${w.bad_lines}</span>` : "");
}
function driftCell(d) {
  return d.map(x => { if (!x.exists) return `<span class="chip">${esc(x.file.split("/").pop())} 없음</span>`; if (x.match === null) return `<span class="chip">${esc(x.file.split("/").pop())} ?</span>`; return `<span class="chip ${x.match ? "ok" : "warn"}" title="${x.size}B vs 템플릿 ${x.template_size}B">${esc(x.file.split("/").pop())} ${x.match ? "=" : "≠"}</span>`; }).join(" ");
}
function markersCell(m, r) {
  const b = [];
  if (m.parked) b.push('<span class="chip bad">PARKED</span>'); if (m.to_delete) b.push('<span class="chip">_to_delete</span>');
  if (m.jira) b.push('<span class="chip">jira</span>'); if (m.daily) b.push('<span class="chip">daily</span>'); if (m.evidence_pack) b.push('<span class="chip">evidence_pack</span>');
  if (m.claude_skills) b.push('<span class="chip">.claude/skills</span>');
  if (r.nested_repos?.length) b.push(`<span class="chip warn" title="${esc(r.nested_repos.join(", "))}">중첩 repo ${r.nested_repos.length}</span>`);
  if (r.locks?.length) b.push(`<span class="chip bad">lock ${r.locks.length}</span>`);
  if (r.logs_xlsx) b.push('<span class="chip">xlsx 로그</span>');
  return b.join(" ");
}

async function renderRules(body, p) {
  const r = await api("GET", `/api/projects/${p.id}/rules`);
  body.innerHTML = `
    <div class="box"><h3>규칙 준수 (템플릿 기준: ${esc(r.template || "루트에 0.PROJECT_TEMPLATE 없음 — 드리프트 비교 불가")})</h3>
      <div class="tblwrap"><table>
        <tr><th style="width:12rem">git</th><td>${r.git === "repo" ? yn(true) + " 저장소" : r.git === "broken" ? '<span class="bad">● .git 손상</span>' : '<span class="bad">○ 없음</span>'} ${r.remotes.length ? r.remotes.map(x => `<span class="chip ok">${esc(x.name)}: ${esc(x.fetch)}</span>`).join(" ") : (r.git === "repo" ? '<span class="chip warn">remote 없음</span>' : "")}</td></tr>
        <tr><th>GLOBAL_RULES / PROJECT_RULES</th><td>${yn(r.global_rules)} GLOBAL_RULES.md &nbsp; ${yn(r.project_rules)} PROJECT_RULES.md &nbsp; ${yn(r.checks_config)} graph/checks_config.json</td></tr>
        <tr><th>대장 (graph/edges.csv)</th><td>${ledgerCell(r.ledger)}</td></tr>
        <tr><th>작업 로그 (logs/*.jsonl)</th><td>${worklogCell(r.worklog)}</td></tr>
        <tr><th>템플릿 드리프트</th><td>${driftCell(r.drift)}</td></tr>
        <tr><th>표식</th><td>${markersCell(r.markers, r) || '<span class="muted">없음</span>'}</td></tr>
      </table></div>
      <div class="actions" style="margin-top:.6rem">
        <button data-run="validate_worklog">validate_worklog.py 실행</button>
        <button data-run="graph_checks">graph_checks.py 실행</button>
        ${r.locks.length ? `<button id="btnCleanLock">lock ${r.locks.length}개 정리</button>` : ""}
      </div>
      <pre id="toolOut" hidden></pre>
    </div><div class="box"><h3>개발 환경 준비</h3><p>ROOT·프로젝트 지침과 선택 스킬을 보존형 계획으로 준비합니다. 파일 연결 확인과 실제 도구 인식은 별도입니다.</p><button id="ctxOpen">개발 환경 준비…</button> <button id="ssOpen">세션 준비·관측…</button></div>`;
  $("#ctxOpen").onclick = () => contextSetupDialog(p).catch(fail);
  $("#ssOpen").onclick = () => sessionDialog(p).catch(fail);
  body.querySelectorAll("[data-run]").forEach(b => b.onclick = async () => {
    const o = $("#toolOut"); o.hidden = false; o.textContent = "실행 중…";
    try { const res = await api("POST", `/api/projects/${p.id}/rules/run`, { tool: b.dataset.run }); o.textContent = `${res.command || ""}\nEXIT=${res.returncode ?? "-"}\n${res.stdout || ""}${res.stderr || ""}${res.error || ""}`; o.className = res.ok ? "ok" : "bad"; } catch (e) { fail(e); }
  });
  const cl = $("#btnCleanLock"); if (cl) cl.onclick = async () => { try { const res = await gitWrite(p,"clean_locks",{}); toast(`제거 ${res.removed.length} / 건너뜀 ${res.skipped.length} ${res.reason}`); await refreshCurrent(); } catch (e) { fail(e); } };
}

function attentionRow() {
  const items = [];
  const add = (label, ps, tab, cls = "warn") => { if (ps.length) items.push(`<span class="chip ${cls}" style="font-size:.85rem;padding:.15rem .6rem">${label} ${ps.length}</span> ${ps.map(x => `<button class="linkish" data-go="${esc(x.id)}" data-tab="${tab}">${esc(x.alias)}</button>`).join(" ")}`); };
  const P = state.projects;
  add("lock 잔류", P.filter(x => x.locks), "rules", "bad");
  add(".git 손상/경로 없음", P.filter(x => x.exists === false || x.git === "broken"), "rules", "bad");
  add("뒤처짐 ↓", P.filter(x => x.behind), "status");
  add("push 대기 ↑", P.filter(x => x.ahead), "status");
  add("미커밋 변경", P.filter(x => x.dirty), "status");
  add("remote 없음", P.filter(x => x.git === "repo" && !x.has_remote), "remotes");
  add("Jira 설정 문제", P.filter(x => state.jira[x.id] && state.jira[x.id].enabled && (state.jira[x.id].schema_ok === false || state.jira[x.id].gitignore_ok === false)), "jira");
  add('fetch 시각 미확인',P.filter(x=>x.git==='repo'&&x.has_remote&&!x.sync?.last_fetch_at),'status');
  return items.length ? `<div class="box" style="padding:.6rem .9rem"><b>주의</b><div style="display:flex;flex-direction:column;gap:.25rem;margin-top:.3rem">${items.map(i => `<div>${i}</div>`).join("")}</div></div>`
                      : '<p class="ok">● 주의할 항목이 없습니다 — 모든 저장소가 깨끗하고 원격과 맞습니다(마지막 fetch 기준).</p>';
}

async function renderWorkspaceDashboard(c) {
  const generation=state.dashboardGeneration=(state.dashboardGeneration||0)+1;
  const contextPromise=api('GET','/api/v1/context/dashboard').catch(e=>({error:e.message}));
  c.innerHTML = `<h2 style="margin:.2rem 0 .6rem">대시보드 <span class="muted" style="font-size:.8rem;font-weight:400">등록 ${state.projects.length}개</span></h2>${attentionRow()}<h3 style="margin:.9rem 0 .3rem">규칙 준수</h3><p class="muted">불러오는 중…</p>`;
  c.querySelectorAll("[data-go]").forEach(b => b.onclick = () => select(b.dataset.go, b.dataset.tab));
  const rows = await Promise.all(state.projects.map(async p => { try { return { p, r: await api("GET", `/api/projects/${p.id}/rules`) }; } catch (e) { return { p, err: e.message }; } }));
  if(state.view !== 'dashboard')return;
  const context=await contextPromise;
  if(state.view !== 'dashboard' || generation!==state.dashboardGeneration)return;
  c.querySelector("p").remove();
  c.insertAdjacentHTML("beforeend", `<div style="overflow:auto"><table>
    <tr><th>프로젝트</th><th>git</th><th>RULES</th><th>대장</th><th>작업 로그</th><th>드리프트</th><th>표식</th></tr>
    ${rows.map(({ p, r, err }) => err ? `<tr><td>${esc(p.alias)}</td><td colspan="6" class="bad">${esc(err)}</td></tr>` : `<tr class="click" data-id="${esc(p.id)}">
      <td><b>${esc(p.alias)}</b><br><span class="muted" style="font-size:.75rem">${esc(p.branch || "")}</span></td>
      <td>${r.git === "repo" ? (r.remotes.length ? '<span class="ok">● remote</span>' : '<span class="warn">● 로컬 전용</span>') : r.git === "broken" ? '<span class="bad">손상</span>' : '<span class="bad">없음</span>'}${p.dirty ? `<br><span class="warn">변경 ${p.dirty}</span>` : ""}</td>
      <td>${yn(r.global_rules)} G ${yn(r.project_rules)} P ${yn(r.checks_config)} C</td>
      <td>${ledgerCell(r.ledger)}</td><td>${worklogCell(r.worklog)}</td><td>${driftCell(r.drift)}</td><td>${markersCell(r.markers, r)}</td></tr>`).join("")}
  </table></div>
  <p class="muted" style="font-size:.8rem">● 있음 / ○ 없음 · G=GLOBAL_RULES, P=PROJECT_RULES, C=checks_config · 드리프트 "="는 루트의 0.PROJECT_TEMPLATE 과 md5 일치</p>`);
  c.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => select(tr.dataset.id, "rules"));
  c.insertAdjacentHTML('beforeend',contextDashboard(context));
  state.workflow=context.workflow||null;
  const workUI=window.wsflowUI.get('work-calendar');
  if(workUI){c.insertAdjacentHTML('beforeend',workUI.workflowDashboard(state.workflow));workUI.bindWorkflow(c);}
  renderList();
  c.querySelectorAll('[data-session-go]').forEach(b=>b.onclick=()=>{const p=state.projects.find(p=>p.id===b.dataset.sessionGo);if(p)sessionDialog(p,b.dataset.tool).catch(fail);});
  const calendar=c.querySelector('[data-calendar-open]');if(calendar)calendar.onclick=()=>window.wsflowUI.invoke('work-calendar','calendar').catch(fail);
}

const recognitionLabel = value => ({unknown:'실제 도구 인식 미확인',agent_reported:'에이전트 보고 · 수동 제출',tool_observed:'도구 출력 관측 · 수동 제출·진위 미검증',partial:'부분 관측 · 전체 인식 미확인'}[value]||'실제 도구 인식 미확인');
function contextDashboard(data) {
  if(data.error||!Array.isArray(data.projects)||!data.calendar)return `<section id="devContextDashboard" class="box"><h3>개발 환경</h3><p class="warn">상태 미확인: ${esc(data.error||'응답 형식을 확인할 수 없습니다')}</p></section>`;
  return `<section id="devContextDashboard" class="box"><h3>개발 환경 · 세션 관측</h3><p class="muted">파일 연결 확인은 실제 도구 로딩·정책 준수의 증명이 아닙니다. 개인/플러그인 범위는 미확인입니다.</p>
    ${data.projects.map(p=>`<div data-context-project="${esc(p.id)}" style="border-top:1px solid #ddd;padding:.6rem 0;overflow-wrap:anywhere"><b>${esc(p.alias)}</b>
      ${Object.entries(p.tools).map(([tool,s])=>`<div data-context-tool="${esc(tool)}"><b>${esc(tool)}</b> · <span class="${s.files.ready?'ok':'warn'}">${s.files.ready?'파일 연결 확인':'파일 준비 차단'}</span> · <span>${recognitionLabel(s.recognition)}</span>
        ${s.packet ? `<span class="muted">${esc(s.packet.profile.version)} / ${esc(s.packet.profile.surface)} · 버전 사용자 선언</span>` : ''}
        ${s.latest_receipt ? `<p class="muted">세션 ${esc(s.latest_receipt.session_ref)} · 관측 ${esc(s.latest_receipt.captured_at)}${s.latest_receipt.stale_reasons.length?' · 오래된 기록: '+s.latest_receipt.stale_reasons.map(esc).join(', '):''}</p>` : ''}
        ${s.files.blockers.length||s.files.warnings.length ? `<details><summary>파일 진단</summary><p>${[...s.files.blockers,...s.files.warnings].map(esc).join('<br>')}</p></details>`:''}
        <button data-session-go="${esc(p.id)}" data-tool="${esc(tool)}">${esc(tool)} 준비·관측</button></div>`).join('')}
      <p class="muted">공통 변경 영향: ${p.impact_project_ids.map(esc).join(', ')||'미확인'}</p></div>`).join('')}</section>
    ${window.wsflowUI.get('work-calendar') ? `<section id="calendarHealth" class="box"><h3>캘린더 동기화</h3><p>${esc(({ok:'연결 상태 확인',attention:'확인 필요',unknown:'상태 미확인',disconnected:'Google 미연결'})[data.calendar.state]||'상태 미확인')}${data.calendar.last_sync?' · 마지막 수집 '+esc(data.calendar.last_sync):''}</p><p>${(data.calendar.warnings||[]).map(esc).join('<br>')}</p><button data-calendar-open>캘린더에서 확인</button></section>` : ''}`;
}

async function sessionDialog(project,initialTool='codex') {
  const dlg=$('#dlg'),pid=project.id;
  let status=await api('GET',`/api/v1/projects/${encodeURIComponent(pid)}/session/status`),tool=initialTool,generation=0;
  state.sessionDrafts=state.sessionDrafts||{};
  const draft=()=>state.sessionDrafts[JSON.stringify([pid,tool])] ||= {version:status.tools[tool].packet?.profile.version||'',surface:status.tools[tool].packet?.profile.surface||'cli',text:'',kind:'agent_reported',attest:false,packet:status.tools[tool].packet};
  const keep=()=>{const d=draft();d.version=$('#ssVersion').value;d.surface=$('#ssSurface').value;d.text=$('#ssEvidence').value;d.kind=$('#ssKind').value;d.attest=$('#ssAttest').checked;};
  const render=()=>{
    const d=draft(),s=status.tools[tool];
    dlg.innerHTML=`<h3>세션 준비·관측 — ${esc(project.alias)}</h3><p>checkout <code style="overflow-wrap:anywhere">${esc(s.files.cwd||project.path)}</code></p>
      <p id="ssDistinction">${s.files.ready?'파일 연결 확인':'파일 준비 차단'} · ${recognitionLabel(s.recognition)}<br><span class="muted">도구를 자동 실행하지 않습니다. 도구 출력은 사용자 제출 자료이며 진위를 독립 검증하지 않습니다.</span></p>
      <label>도구 <select id="ssTool"><option value="codex" ${tool==='codex'?'selected':''}>Codex</option><option value="claude" ${tool==='claude'?'selected':''}>Claude Code</option></select></label>
      <label>실제 사용 버전 <input id="ssVersion" value="${esc(d.version)}" maxlength="100" placeholder="사용자가 확인한 버전"></label>
      <label>환경 <select id="ssSurface">${['cli','desktop','ide'].map(v=>`<option ${v===d.surface?'selected':''}>${v}</option>`).join('')}</select></label>
      <div class="actions"><button id="ssPrepare">이 환경의 새 세션 준비</button><button id="ssRefresh">상태 다시 확인</button><button id="ssInit">파일 준비 열기</button></div>
      <p class="warn">${s.files.blockers.map(esc).join('; ')}</p><div id="ssPrepared"></div>
      <details><summary>관측 자료 제출</summary><p>실제로 확인한 출처와 스킬만 JSON에 기록합니다. 자체 보고는 에이전트 보고로 남깁니다. 도구 출력 원본을 제출한 경우에만 도구 관측을 선택하세요.</p>
        <button id="ssTemplate">빈 관측 양식 넣기</button><label>관측 종류 <select id="ssKind">${[['agent_reported','에이전트 보고'],['tool_observed','도구 출력 관측'],['static_only','파일 확인 기록'],['unknown','미확인 기록']].map(([v,label])=>`<option value="${v}" ${v===d.kind?'selected':''}>${label}</option>`).join('')}</select></label>
        <textarea id="ssEvidence" rows="9" style="width:100%;box-sizing:border-box" placeholder="관측 JSON — 지시·코드는 실행하지 않습니다">${esc(d.text)}</textarea>
        <label style="display:block"><input type="checkbox" id="ssAttest" ${d.attest?'checked':''}> 실제 도구가 제공한 출력임을 확인했습니다 (진위 독립검증 아님).</label>
        <button id="ssImport">이 준비 패킷에 관측 기록</button></details>
      <p id="ssMessage" role="status"></p><div id="ssHistory"><h4>관측 이력</h4>${s.receipts.map(r=>`<div class="box"><b>${recognitionLabel(r.recognition)}</b> · ${esc(r.evidence_kind)}<br>세션 ${esc(r.session_ref)} · ${esc(r.profile.tool)} ${esc(r.profile.version)} / ${esc(r.profile.surface)} · ${esc(r.captured_at)}<br>관측 문서 ${r.verification_scope.sources.length}개 / 스킬 ${r.verification_scope.skills.length}개 · ${r.verification_scope.selected_scope_complete?'준비 범위 전체 보고':'부분/미확인'}<p class="warn">${r.stale_reasons.length?'오래된 기록: '+r.stale_reasons.map(esc).join(', '):'사용자 제출 자료 · 정책 준수/개인 전역 범위 미검증'}</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(r.notes)}</pre><details><summary>제출 근거·범위</summary><p style="overflow-wrap:anywhere">${esc(r.evidence_path)}<br>sha256 ${esc(r.evidence_sha256)}<br>snapshot ${esc(r.snapshot_id)}<br>checkout ${esc(r.cwd)}</p></details></div>`).join('')||'<p class="muted">관측 없음 · 실제 인식 미확인</p>'}</div><button id="ssClose">닫기</button>`;
    const packetView=()=>{
      const packet=draft().packet;
      $('#ssPrepared').innerHTML=packet?`<p>준비 패킷 · ${esc(packet.profile.tool)} ${esc(packet.profile.version)} · ${esc(packet.snapshot_id)}<br>파일 기준 준비이며 실제 인식은 미확인입니다.${packet.stale_reasons?.length?' 오래된 패킷: '+packet.stale_reasons.map(esc).join(', '):''}</p><textarea id="ssPacketText" rows="7" readonly style="width:100%;box-sizing:border-box">${esc(packet.startup_text)}</textarea><button id="ssCopy">시작 정보 복사</button>`:'';
      $('#ssImport').disabled=!packet;$('#ssTemplate').disabled=!packet;
      if(packet)$('#ssCopy').onclick=async()=>{try{await navigator.clipboard.writeText(packet.startup_text);$('#ssMessage').textContent='시작 정보를 복사했습니다. 해당 checkout에서 새 세션을 여세요.';}catch{$('#ssPacketText').select();$('#ssMessage').textContent='시작 정보를 선택했습니다. 복사해 사용하세요.';}};
    };
    packetView();
    const invalidate=()=>{keep();generation++;draft().packet=null;packetView();$('#ssDistinction').textContent='선택 환경 변경 · 새 준비 필요 · 실제 도구 인식 미확인';$('#ssMessage').textContent='환경이 바뀌었습니다. 새 준비 패킷을 만드세요.';};
    $('#ssVersion').oninput=invalidate;$('#ssSurface').onchange=invalidate;
    $('#ssEvidence').oninput=keep;$('#ssKind').onchange=keep;$('#ssAttest').onchange=keep;
    $('#ssTool').onchange=()=>{keep();generation++;tool=$('#ssTool').value;render();};
    $('#ssPrepare').onclick=async()=>{keep();const d=draft(),captured=++generation,body={tool,version:d.version,surface:d.surface};$('#ssPrepare').disabled=true;$('#ssMessage').textContent='새 세션 준비 중…';
      try{const p=await api('POST',`/api/v1/projects/${encodeURIComponent(pid)}/session/prepare`,body);if(captured!==generation||!dlg.open)return;
        if(!p.ready){$('#ssMessage').textContent='준비 차단: '+p.blockers.join('; ');return;}
        d.packet=p;packetView();$('#ssDistinction').textContent='파일 기준 새 세션 준비 · 실제 도구 인식 미확인';$('#ssHistory').textContent='새 패킷의 관측은 없습니다. 이전 이력을 다시 확인하는 중입니다.';
        const next=await api('GET',`/api/v1/projects/${encodeURIComponent(pid)}/session/status`);if(captured!==generation||!dlg.open)return;status=next;render();$('#ssMessage').textContent='파일 기준 세션 준비 완료 · 실제 도구 인식 미확인';
      }catch(e){if(captured===generation)$('#ssMessage').textContent=e.message;}finally{if(dlg.open&&$('#ssPrepare'))$('#ssPrepare').disabled=false;}};
    $('#ssTemplate').onclick=()=>{keep();const d=draft(),p=d.packet;if(d.text.trim()){$('#ssMessage').textContent='기존 입력을 보존했습니다. 새 양식은 입력을 비운 뒤 넣으세요.';return;}
      d.text=JSON.stringify(p.observation_template||{schema_version:1,origin:'agent_report',session_ref:'실제 세션 식별자',captured_at:new Date().toISOString(),snapshot_id:p.snapshot_id,profile_id:p.profile.profile_id,cwd:p.cwd,observed_sources:[],observed_skills:[],notes:'실제로 확인한 항목만 기록하세요.'},null,2);$('#ssEvidence').value=d.text;};
    $('#ssImport').onclick=async()=>{keep();const d=draft(),packet=d.packet,captured=++generation;if(!packet)return;const submitted={text:d.text,kind:d.kind,attest:d.attest};$('#ssImport').disabled=true;$('#ssMessage').textContent='관측 기록 중…';
      try{const e=await api('POST',`/api/v1/projects/${encodeURIComponent(pid)}/session/evidence`,{content:submitted.text});
        if(captured!==generation||!dlg.open)return;
        const r=await api('POST',`/api/v1/projects/${encodeURIComponent(pid)}/session/observations`,{packet_id:packet.packet_id,evidence_id:e.evidence_id,evidence_kind:submitted.kind,attest_tool_output:submitted.attest});
        if(captured!==generation||!dlg.open)return;status=await api('GET',`/api/v1/projects/${encodeURIComponent(pid)}/session/status`);if(captured!==generation||!dlg.open)return;render();$('#ssMessage').textContent='관측 기록: '+recognitionLabel(r.recognition)+(r.stale_reasons.length?' · 오래된 기록: '+r.stale_reasons.join(', '):'');
      }catch(e){if(captured===generation&&dlg.open){$('#ssMessage').textContent=e.message;$('#ssImport').disabled=false;}}};
    $('#ssRefresh').onclick=async()=>{keep();const captured=++generation;try{const next=await api('GET',`/api/v1/projects/${encodeURIComponent(pid)}/session/status`);if(captured!==generation||!dlg.open)return;status=next;const p=status.tools[tool].packet;draft().packet=p&&p.profile.version===draft().version&&p.profile.surface===draft().surface?p:null;render();}catch(e){if(captured===generation)$('#ssMessage').textContent=e.message;}};
    $('#ssInit').onclick=()=>{keep();generation++;dlg.close();contextSetupDialog(project).catch(fail);};
    $('#ssClose').onclick=()=>{keep();generation++;dlg.close();};
  };
  render();dlg.showModal();
}

// ------------------------------------------------------------- 스캔 / lock 대화상자
async function scanDialog() {
  const root = $("#rootSel").value; if (!root) return toast("먼저 루트를 추가하세요");
  const dlg = $("#dlg"); dlg.innerHTML = "<p>스캔 중…</p>"; dlg.showModal();
  try {
    const r = await api("GET", `/api/scan?root=${encodeURIComponent(root)}`);
    dlg.innerHTML = `<h3 style="margin-top:0">${esc(root)} — 폴더 ${r.items.length}개</h3>
      <div style="max-height:55vh;overflow:auto"><table><tr><th></th><th>폴더</th><th>git</th><th>규칙</th><th>로그</th><th></th></tr>
      ${r.items.map(i => `<tr><td><input type="checkbox" value="${esc(i.path)}" ${i.registered ? "disabled" : (i.git === "repo" || i.project_rules ? "checked" : "")}></td><td>${esc(i.name)}${i.parked ? ' <span class="chip bad">PARKED</span>' : ""}</td>
        <td>${i.git === "repo" ? '<span class="ok">●</span>' : i.git === "broken" ? '<span class="bad">손상</span>' : '<span class="muted">○</span>'}</td>
        <td>${i.global_rules ? "G" : ""} ${i.project_rules ? "P" : ""} ${i.ledger ? "L" : ""}</td><td>${i.logs_jsonl ? "jsonl" : ""} ${i.logs_xlsx ? "xlsx" : ""}</td><td>${i.registered ? '<span class="muted">등록됨</span>' : ""}</td></tr>`).join("")}</table></div>
      <div class="actions" style="justify-content:flex-end;margin-top:.6rem"><button id="dlgAll">전체 선택</button><button id="dlgClose">닫기</button><button class="primary" id="dlgReg">선택 등록</button></div>`;
    $("#dlgAll").onclick = () => dlg.querySelectorAll("input[type=checkbox]:not(:disabled)").forEach(c => c.checked = true);
    $("#dlgClose").onclick = () => dlg.close();
    $("#dlgReg").onclick = async () => {
      const paths = [...dlg.querySelectorAll("input[type=checkbox]:checked:not(:disabled)")].map(c => c.value);
      let ok = 0; for (const path of paths) { try { await api("POST", "/api/projects", { path }); ok++; } catch (e) { toast(e.message); } }
      dlg.close(); toast(`${ok}개 등록`); await loadAll();
    };
  } catch (e) { dlg.innerHTML = `<p class="bad">${esc(e.message)}</p><button onclick="this.closest('dialog').close()">닫기</button>`; }
}

// ------------------------------------------------------------- 보존형 개발 환경 준비 (I5)
async function contextSetupDialog(project) {
  const pid = project.id, dlg = $("#dlg");
  const data = await api("GET", `/api/v1/projects/${encodeURIComponent(pid)}/context/setup`);
  let plan = null, generation = 0;
  dlg.innerHTML = `<h3>개발 환경 준비 — ${esc(project.alias)}</h3>
    <p>ROOT: <code>${esc(data.root_path)}</code><br>checkout: <code>${esc(data.checkout_path)}</code></p>
    <p id="ctxObservation" class="muted">파일 기준 조사 · 실제 Codex/Claude 인식: 미검증. 기존 .codex/.claude와 스킬 원본은 보존합니다.</p>
    <p>${data.root_initialized ? 'ROOT 프로필이 있습니다.' : '먼저 ROOT 초기화 계획을 확인하고 적용하세요.'} 공통 변경 영향은 ROOT 계획에서 표시됩니다.</p>
    <label>노출 방식 <select id="ctxBackend"><option value="">선택하세요</option><option value="managed-copy">관리 사본 (원본과 별개, 변경 감지)</option></select></label>
    <div style="max-height:24vh;overflow:auto">${data.catalog.map((s,i) => `<label style="display:block"><input type="checkbox" data-ctx-skill="${i}" value="${esc(s.skill_id)}" ${data.project_profile?.selected_skills?.includes(s.skill_id) ? 'checked' : ''}> ${esc(s.skill_id)} · 전체 ${s.file_count}파일 <code>${esc(s.tree_hash.slice(0,12))}</code></label>`).join('') || '<p class="muted">ROOT/skills와 프로젝트/skills에 등록된 패키지가 없습니다. 기존 도구별 패키지는 자동 이관하지 않습니다.</p>'}</div>
    <label style="display:block;margin:.6rem 0"><input type="checkbox" id="ctxAdopt"> 기존 진입점에 관리 블록 추가·유효 프로필 채택을 허용합니다 (원문 보존).</label>
    <details><summary>기존 경로·진단</summary><pre style="white-space:pre-wrap">${esc([...data.diagnostics.map(d=>d.path+' — '+d.policy),...data.blockers].join('\n'))}</pre></details>
    <div class="actions"><button id="ctxRootPreview">ROOT 초기화 미리보기</button><button id="ctxPreview">프로젝트 준비 미리보기</button></div>
    <div id="ctxPlan" style="max-height:32vh;overflow:auto;overflow-wrap:anywhere"></div><p id="ctxStatus" role="status"></p>
    <div class="actions"><button id="ctxApply" class="primary" disabled>이 계획 적용</button><button id="ctxRetry" hidden>같은 승인 확인</button><button id="ctxClose">닫기</button></div>
    <div id="ctxOperations">${data.operations.filter(o=>['planned','executing','uncertain'].includes(o.status)).map((o,i)=>`<p><code>${esc(o.operation_id)}</code> ${esc(o.status)} <button data-ctx-op="${i}">계획·복구 확인</button></p>`).join('')}</div>`;
  const active = data.operations.filter(o=>['planned','executing','uncertain'].includes(o.status));
  const invalidate = () => { generation++; plan=null; $("#ctxApply").disabled=true; $("#ctxRetry").hidden=true; $("#ctxPlan").textContent='선택이 바뀌었습니다. 다시 미리보기하세요.'; };
  dlg.querySelectorAll('input,select').forEach(el=>el.onchange=invalidate);
  const show = (p, op=null) => {
    plan={...p, operation_id:op?.operation_id||p.operation_id, recover:op && ['executing','uncertain'].includes(op.status)};
    $("#ctxPlan").innerHTML=`<p><b>${esc(p.kind)}</b> · 대상 ${esc(p.owner)}<br>영향 프로젝트: ${esc(p.impact_project_ids.join(', '))}</p>
      ${p.blockers.length ? `<p class="bad">차단: ${p.blockers.map(esc).join('; ')}</p>` : ''}
      <p>변경 ${p.actions.length}개 항목 · 파일별 백업/저널 · 실제 도구 인식 미검증</p>${p.actions.map(a=>`<details><summary>${a.directory ? '디렉터리 준비' : a.data===null ? '소유 파일 제거' : '생성/갱신'}: ${esc(a.rel)}</summary>${a.diff ? `<p>이전</p><pre style="white-space:pre-wrap">${esc(a.diff.before)}</pre><p>적용 후</p><pre style="white-space:pre-wrap">${esc(a.diff.after)}</pre>` : '전체 패키지 해시로 검증하는 항목입니다.'}</details>`).join('')}`;
    $("#ctxApply").textContent=plan.recover ? '이 작업 명시 복구' : '이 계획 적용';
    $("#ctxApply").disabled=p.blockers.length>0 || !p.plan_id;
    $("#ctxRetry").hidden=true;
  };
  const preview = async kind => {
    const captured=++generation;plan=null;$("#ctxApply").disabled=true;$("#ctxRetry").hidden=true;
    const body={kind,backend:$("#ctxBackend").value,selected_skills:[...dlg.querySelectorAll('[data-ctx-skill]:checked')].map(x=>x.value),adopt_existing:$("#ctxAdopt").checked};
    try {
      const p=await api('POST',kind==='root_init' ? '/api/v1/roots/context/plan' : `/api/v1/projects/${encodeURIComponent(pid)}/context/plan`,kind==='root_init' ? {...body,project_id:pid} : body);
      if(captured!==generation || !dlg.open)return;
      show(p);$("#ctxStatus").textContent='미리보기만 수행했습니다. 적용할 내용을 확인하세요.';
    } catch(e) { if(captured===generation)$("#ctxStatus").textContent=e.message; }
  };
  const apply = async () => {
    if(!plan)return;const saved=plan;$("#ctxApply").disabled=true;$("#ctxRetry").hidden=true;
    try {
      const r=await api('POST',`/api/v1/context/operations/${encodeURIComponent(saved.operation_id)}/${saved.recover ? 'recover' : 'apply'}`,{plan_id:saved.plan_id,project_id:pid});
      if(r.status==='succeeded') { dlg.close();toast('개발 환경 파일 준비 완료 · 실제 도구 인식 미검증');await contextSetupDialog(project); }
      else { $("#ctxStatus").textContent='일부 적용 후 중단: '+(r.error||'결과 확인 필요')+' — 같은 작업의 복구 계획을 확인하세요.';saved.recover=true;$("#ctxApply").textContent='이 작업 명시 복구';$("#ctxApply").disabled=false; }
    } catch(e) { $("#ctxStatus").textContent='응답을 확인하지 못했습니다: '+e.message+' — 같은 승인 ID로 확인할 수 있습니다.';$("#ctxRetry").hidden=false; }
  };
  $("#ctxRootPreview").onclick=()=>preview('root_init');$("#ctxPreview").onclick=()=>preview('project_init');$("#ctxApply").onclick=apply;$("#ctxRetry").onclick=apply;
  dlg.querySelectorAll('[data-ctx-op]').forEach(b=>b.onclick=async()=>{try {const op=await api('GET',`/api/v1/context/operations/${encodeURIComponent(active[+b.dataset.ctxOp].operation_id)}?project_id=${encodeURIComponent(pid)}`);show(op.plan,op);}catch(e){$("#ctxStatus").textContent=e.message;}});
  $("#ctxClose").onclick=()=>{generation++;dlg.close();};
  dlg.showModal();
}

// ------------------------------------------------------------- 새 프로젝트 (D-010: 빈 폴더 + 등록만)
function newProjectDialog() {
  const root = $("#rootSel").value; if (!root) return toast("먼저 루트를 추가하세요");
  const dlg = $("#dlg");
  dlg.innerHTML = `<h3 style="margin-top:0">새 프로젝트 폴더</h3>
    <p class="muted" style="font-size:.85rem">위치: <code>${esc(root)}</code> 아래. 빈 폴더를 만들고 등록합니다 — 템플릿 파일은 "배포"로 넣으세요.</p>
    <input type="text" id="npName" placeholder="폴더 이름" style="width:100%">
    <div class="actions" style="justify-content:flex-end;margin-top:.6rem"><button id="dlgClose">닫기</button><button class="primary" id="npCreate">생성 + 등록</button></div>`;
  dlg.showModal(); $("#npName").focus();
  $("#dlgClose").onclick = () => dlg.close();
  const go = async () => { const name = $("#npName").value.trim(); if (!name) return;
    try { const p = await api("POST", "/api/projects/new", { root, name }); dlg.close(); toast(`생성: ${p.path}`); await loadAll(); select(p.id); } catch (e) { fail(e); } };
  $("#npCreate").onclick = go; $("#npName").onkeydown = e => { if (e.key === "Enter") go(); };
}

// ------------------------------------------------------------- 배포 (D-011: 항상 덮어쓰기, 백업 없음, 실행 전 미리보기)
state.deploy = { sources: [] };
async function deployDialog() {
  const dlg = $("#dlg"); const d = state.deploy;
  const render = () => {
    dlg.innerHTML = `<h3 style="margin-top:0">배포 — 폴더/파일을 프로젝트들에 복사</h3>
      <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap"><b>원본</b>
        <button id="dpAddDir">폴더 추가…</button><button id="dpAddFiles">파일 추가…</button><button id="dpClear">비우기</button>
        <span class="muted" style="font-size:.8rem">루트의 0.PROJECT_TEMPLATE 아래 원본은 같은 상대경로로, 그 외는 아래 하위경로로 복사</span></div>
      <ul id="dpSrc" style="margin:.4rem 0;max-height:18vh;overflow:auto;font-size:.85rem">${d.sources.map((s, i) => `<li><code>${esc(s)}</code> <a href="#" data-i="${i}" class="dpRm">✕</a></li>`).join("") || '<li class="muted">원본 없음</li>'}</ul>
      <input type="text" id="dpDest" placeholder="템플릿 밖 원본의 대상 하위경로 (비우면 프로젝트 루트)" style="width:100%" value="${esc(d.dest_rel || "")}">
      <div style="margin-top:.6rem"><b>대상 프로젝트</b> <button id="dpAll">전체</button> <button id="dpNone">해제</button></div>
      <div style="max-height:22vh;overflow:auto;columns:2;font-size:.85rem">${state.projects.filter(p => p.exists).map(p => `<label style="display:block"><input type="checkbox" value="${esc(p.id)}" ${d.checked?.includes(p.id) ? "checked" : ""}> ${esc(p.alias)}${p.git === "none" ? ' <span class="muted">(git 없음)</span>' : ""}</label>`).join("")}</div>
      <div id="dpPlan" style="margin-top:.6rem"></div>
      <div class="actions" style="justify-content:flex-end;margin-top:.6rem"><button id="dlgClose">닫기</button><button id="dpPreview" class="primary">미리보기</button><button class="danger" id="dpApply" disabled title="미리보기 뒤에 실행할 수 있습니다">배포</button></div>`;
    $("#dlgClose").onclick = () => { d.checked = checked(); d.dest_rel = $("#dpDest").value; dlg.close(); };
    const checked = () => [...dlg.querySelectorAll("input[type=checkbox]:checked")].map(c => c.value);
    $("#dpAll").onclick = () => dlg.querySelectorAll("input[type=checkbox]").forEach(c => c.checked = true);
    $("#dpNone").onclick = () => dlg.querySelectorAll("input[type=checkbox]").forEach(c => c.checked = false);
    dlg.querySelectorAll(".dpRm").forEach(a => a.onclick = e => { e.preventDefault(); d.sources.splice(+a.dataset.i, 1); keep(); render(); });
    const keep = () => { d.checked = checked(); d.dest_rel = $("#dpDest").value; };
    $("#dpClear").onclick = () => { d.sources = []; keep(); render(); };
    $("#dpAddDir").onclick = async () => { keep(); try { const r = await api("POST", "/api/pick-folder", { initial: $("#rootSel").value || "" }); if (!r.cancelled && !d.sources.includes(r.path)) d.sources.push(r.path); render(); } catch (e) { fail(e); } };
    $("#dpAddFiles").onclick = async () => { keep(); try { const r = await api("POST", "/api/pick-files", { initial: $("#rootSel").value || "" }); for (const p of r.paths) if (!d.sources.includes(p)) d.sources.push(p); render(); } catch (e) { fail(e); } };
    const body = () => ({ sources: d.sources, ids: checked(), dest_rel: $("#dpDest").value.trim() });
    $("#dpPreview").onclick = async () => {
      try { const r = await api("POST", "/api/deploy/plan", body()); showPlan(r); $("#dpApply").disabled = !(r.total.new + r.total.changed); } catch (e) { fail(e); }
    };
    $("#dpApply").onclick = async () => {
      const b = body(); const r0 = await api("POST", "/api/deploy/plan", b).catch(fail); if (!r0) return;
      keep();
      // 확인 대화상자가 같은 <dialog> 를 쓰므로, 답을 받은 뒤 배포 창을 다시 그려 연다.
      const ok = await confirmDanger("배포 — 파일을 덮어씁니다", [["대상", `${b.ids.length}개 프로젝트`], ["신규", `${r0.total.new} 파일`], ["덮어쓰기", `<b class="bad">${r0.total.changed} 파일</b>`], ["되돌리기", "백업을 만들지 않습니다 — git 에 커밋된 파일만 되돌릴 수 있습니다"]], "배포");
      render(); dlg.showModal(); if (!ok) { showPlan(r0); $("#dpApply").disabled = !(r0.total.new + r0.total.changed); return; }
      try { const r = await api("POST", "/api/deploy/apply", b); toast(`복사 ${r.copied_total}건` + (r.results.some(x => x.failed.length) ? " (실패 있음 — 아래 참조)" : ""));
        $("#dpPlan").innerHTML = `<table><tr><th>프로젝트</th><th>복사</th><th>동일(건너뜀)</th><th>실패/오류</th></tr>${r.results.map(x => `<tr><td>${esc(x.alias)}</td><td>${x.copied.length}</td><td>${x.skipped_same}</td><td class="${x.failed.length || x.errors.length ? "bad" : ""}">${esc([...x.failed.map(f => f.rel + ": " + f.error), ...x.errors].join("; "))}</td></tr>`).join("")}</table>`;
        $("#dpApply").disabled = true; await loadAll(); } catch (e) { fail(e); }
    };
  };
  const showPlan = r => {
    $("#dpPlan").innerHTML = `<table><tr><th>프로젝트</th><th class="ok">신규</th><th>변경(덮어쓰기)</th><th class="muted">동일</th><th>오류</th></tr>
      ${r.projects.map(p => `<tr><td>${esc(p.alias)}</td><td>${p.counts.new}</td><td>${p.counts.changed}</td><td class="muted">${p.counts.same}</td><td class="bad">${esc(p.errors.join("; "))}</td></tr>
        ${p.items.some(i => i.status !== "same") ? `<tr><td colspan="5"><details><summary class="muted" style="font-size:.8rem">파일 보기</summary><div style="font-size:.8rem;max-height:14vh;overflow:auto">${p.items.filter(i => i.status !== "same").map(i => `<div><span class="${i.status === "new" ? "ok" : ""}">${i.status === "new" ? "＋" : "↻"}</span> <code>${esc(i.rel)}</code></div>`).join("")}</div></details></td></tr>` : ""}`).join("")}
      <tr><th>합계</th><th>${r.total.new}</th><th>${r.total.changed}</th><th class="muted">${r.total.same}</th><th></th></tr></table>`;
  };
  render(); dlg.showModal();
}

async function locksDialog() {
  const dlg = $("#dlg"); dlg.innerHTML = "<p>검사 중…</p>"; dlg.showModal();
  try {
    const r = await api("GET", "/api/locks");
    const hit = r.locks.filter(x => x.locks.length);
    dlg.innerHTML = `<h3 style="margin-top:0">.git lock 잔류 검사 — 저장소 ${r.locks.length}개 중 ${hit.length}개</h3>
      ${hit.length ? `<table><tr><th>프로젝트</th><th>lock 파일</th><th>경과</th></tr>${hit.flatMap(x => x.locks.map(l => `<tr><td>${esc(x.alias)}</td><td><code>${esc(l.path)}</code></td><td>${l.age_sec}s</td></tr>`)).join("")}</table>
        <p class="muted" style="font-size:.85rem">git 프로세스가 실행 중이 아니고 5초 이상 지난 lock 만 삭제합니다. (에이전트 세션이 삭제 권한 없이 git 을 돌릴 때 남는 파일입니다.)</p>` : '<p class="ok">잔류 lock 이 없습니다.</p>'}
      <div class="actions" style="justify-content:flex-end"><button id="dlgClose">닫기</button>${hit.length ? '<button class="primary" id="dlgClean">전부 정리</button>' : ""}</div>`;
    $("#dlgClose").onclick = () => dlg.close();
    const b = $("#dlgClean"); if (b) b.onclick = async () => { try { const res = await api("POST", "/api/locks/clean", { ids: hit.map(x => x.id) }); const rm = res.results.reduce((n, x) => n + x.removed.length, 0), sk = res.results.reduce((n, x) => n + x.skipped.length, 0); toast(`제거 ${rm} / 건너뜀 ${sk}` + (res.results.find(x => x.reason)?.reason ? "\n" + res.results.find(x => x.reason).reason : "")); dlg.close(); await loadAll(); } catch (e) { fail(e); } };
  } catch (e) { dlg.innerHTML = `<p class="bad">${esc(e.message)}</p><button onclick="this.closest('dialog').close()">닫기</button>`; }
}

function workspaceBadges(p,chip) {
  if(p.git==='none')chip('','git 없음','rules','git 저장소가 아닙니다 — 규칙 탭만 쓸 수 있습니다');
  else if(p.git==='broken')chip('bad','.git 손상','rules','.git 이 비었거나 손상됐습니다');
  else if(p.git==='repo'){
    if(!p.has_remote)chip('warn','remote 없음','remotes','원격 백업이 없습니다 — remote 탭에서 추가');
    if(p.dirty)chip('warn',`변경 ${p.dirty}`,'status','커밋되지 않은 변경 — 상태 탭');
    if(p.ahead)chip('warn',`↑${p.ahead} push 대기`,'status','원격에 없는 커밋 — push 하십시오');
    if(p.behind)chip('warn',`↓${p.behind}`,'status','원격이 앞서 있습니다 (마지막 fetch 기준) — 상태 탭에서 ff-only pull');
  }
  if(p.locks)chip('bad',`lock ${p.locks}`,'rules','잔류 .git/*.lock — 규칙 탭에서 정리');
  if(p.parked)chip('','PARKED',null,'_PARKED.txt 표식');
}

window.wsflowUI.register('workspace-operations',{
  tabs:[{id:'status',label:'상태 / 커밋 / push',order:10,gitOnly:true,render:renderStatus},{id:'log',label:'로그',order:20,gitOnly:true,render:renderLog},{id:'remotes',label:'remote',order:30,gitOnly:true,render:renderRemotes},{id:'rules',label:'규칙',order:40,render:renderRules}],
  dashboard:renderWorkspaceDashboard,badges:workspaceBadges,scan:scanDialog,locks:locksDialog,newProject:newProjectDialog,deploy:deployDialog,
  onReady(){
$("#btnAddRoot").onclick = async () => { const path = $("#rootInput").value.trim(); if (!path) return; try { await api("POST", "/api/roots", { path }); $("#rootInput").value = ""; closeMenus(); toast(`루트 추가: ${path}`); await loadAll(); $("#rootSel").value = [...$("#rootSel").options].map(o => o.value).find(v => v.toLowerCase().replace(/\\/g, "/").includes(path.toLowerCase().replace(/\\/g, "/").replace(/\/$/, ""))) || $("#rootSel").value; } catch (e) { fail(e); } };
$("#btnDelRoot").onclick = async () => { const path = $("#rootSel").value; if (!path) return toast("선택된 루트가 없습니다");
  if (!await confirmDanger("루트 제거", [["루트", `<code>${esc(path)}</code>`], ["되돌리기", "목록에서만 빠집니다 — 등록된 프로젝트와 폴더는 그대로입니다"]], "제거")) return;
  try { await api("DELETE", "/api/roots", { path }); toast(`루트 제거: ${path}`); await loadAll(); } catch (e) { fail(e); } };
$("#btnAddProj").onclick = async () => { const path = $("#projInput").value.trim(); if (!path) return; try { const p = await api("POST", "/api/projects", { path }); $("#projInput").value = ""; closeMenus(); toast(`등록: ${p.alias}`); await loadAll(); select(p.id); } catch (e) { fail(e); } };
// 폴더 선택 창은 서버(로컬 PC)가 띄운다 — 브라우저는 선택 폴더의 전체 경로를 주지 않기 때문.
async function pickInto(inputSel, addBtnSel, initialSel) {
  try {
    const r = await api("POST", "/api/pick-folder", { initial: $(initialSel)?.value || "" });
    if (r.cancelled) return;
    $(inputSel).value = r.path;
    $(addBtnSel).click();
  } catch (e) { fail(e); }
}
$("#rootInput").onkeydown = e => { if (e.key === "Enter") $("#btnAddRoot").click(); };
$("#projInput").onkeydown = e => { if (e.key === "Enter") $("#btnAddProj").click(); };
$("#btnPickRoot").onclick = () => pickInto("#rootInput", "#btnAddRoot", "#rootSel");
$("#btnPickProj").onclick = () => pickInto("#projInput", "#btnAddProj", "#rootSel");
  }
});
