// build.js — 영상 생성 탭: 빌드, YouTube 업로드, BAND 게시

// ─── 생성 탭 렌더 ───────────────────────────────────────
function renderBuild() {
  const jobs = allJobs.filter(j => j.n_approved !== null && j.n_approved > 0);
  const h = JSON.stringify(jobs.map(j =>
    [j.id, j.status, j.n_approved, j.output, j.progress, j.yt_status, j.yt_url, j.band_status]));

  // 현재 입력값 수집 (재렌더 여부와 무관하게 항상 최신 상태로 유지)
  document.querySelectorAll("[data-bd-title]").forEach(i => {
    if (!buildData[i.dataset.bdTitle]) buildData[i.dataset.bdTitle] = {};
    buildData[i.dataset.bdTitle].title = i.value;
  });
  document.querySelectorAll("[data-bd-out]").forEach(i => {
    if (!buildData[i.dataset.bdOut]) buildData[i.dataset.bdOut] = {};
    buildData[i.dataset.bdOut].output = i.value;
  });

  // 제목/파일명 입력 중이면 재렌더를 건너뛰어 커서 위치·포커스를 보존한다.
  // (입력값은 위에서 이미 buildData로 수집했으므로 데이터 손실은 없다.)
  const active = document.activeElement;
  if (active && (active.hasAttribute("data-bd-title") || active.hasAttribute("data-bd-out"))) return;

  if (h === lastBuildHash) return;
  lastBuildHash = h;

  const el = document.getElementById("build-list");
  if (!jobs.length) {
    el.innerHTML = '<div class="hint" style="padding:24px 0;text-align:center">리뷰 탭에서 구간을 선택하고 저장하면 여기 나타납니다.</div>';
    // BAND 섹션 숨기기
    const bc = document.getElementById("band-post-card");
    if (bc) bc.style.display = "none";
    return;
  }

  let rows = "";
  for (const j of jobs) {
    if (!buildData[j.id]) buildData[j.id] = {};
    if (!buildData[j.id].title)
      buildData[j.id].title = DEFAULT_TITLE;
    if (!buildData[j.id].output)
      buildData[j.id].output = "highlights_" + j.video_name.replace(/\.[^.]+$/, "") + ".mp4";

    const prog = j.progress || {};
    // 영상 생성 상태
    let stHtml = stBadge(j.status);
    if (j.status === "done" && j.output) {
      stHtml = `<span class="st st-done">완료</span>
        <a href="/api/jobs/${j.id}/output" style="color:var(--blue);font-size:12px;margin-left:6px">다운로드</a>`;
    } else if (j.status === "building" && prog.total > 0) {
      const pct = Math.round(prog.done / prog.total * 100);
      stHtml += `<div class="bar" style="margin-top:4px;width:120px"><i style="width:${pct}%"></i></div>
        <span class="hint">${prog.done}/${prog.total} (${pct}%)</span>`;
    }

    // YouTube 상태 셀
    let ytHtml = '<span class="hint" style="color:var(--rej)">-</span>';
    if (j.status === "done" && j.output) {
      if (!j.yt_status) {
        ytHtml = ytAuth
          ? `<button class="ghost sm" onclick="doUploadOne('${j.id}')">업로드</button>`
          : `<span class="hint">미인증</span>`;
      } else if (j.yt_status === "uploading") {
        const yp = j.yt_progress || {};
        const pct = yp.total > 0 ? Math.round(yp.done / yp.total * 100) : 0;
        ytHtml = `<span class="spin"></span><span class="hint">${pct}%</span>`;
      } else if (j.yt_status === "done" && j.yt_url) {
        ytHtml = `<a href="${esc(j.yt_url)}" target="_blank"
          style="color:var(--accent);font-size:12px;word-break:break-all">${esc(j.yt_url)}</a>`;
      } else if (j.yt_status === "error") {
        ytHtml = `<span style="color:var(--red);font-size:11px" title="${esc(j.yt_error||'')}">오류 ⚠</span>
          <button class="ghost sm" onclick="doUploadOne('${j.id}')">재시도</button>`;
      }
    }

    // BAND 상태 셀
    let bandHtml = '<span class="hint" style="color:var(--rej)">-</span>';
    if (j.yt_status === "done" && j.yt_url) {
      if (!j.band_status) {
        bandHtml = `<span class="hint" style="font-size:11px">BAND 게시 대기</span>`;
      } else if (j.band_status === "done" && j.band_post_url) {
        bandHtml = `<a href="${esc(j.band_post_url)}" target="_blank"
          style="color:var(--purple);font-size:12px">게시됨 ↗</a>`;
      } else if (j.band_status === "error") {
        bandHtml = `<span style="color:var(--red);font-size:11px" title="${esc(j.band_error||'')}">오류 ⚠</span>`;
      }
    }

    rows += `<tr>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;padding-right:8px"
          title="${esc(j.video_name)}">${esc(j.video_name)}</td>
      <td style="font-weight:700;color:var(--accent);text-align:center">${j.n_approved}</td>
      <td><input type="text" data-bd-title="${j.id}" value="${esc(buildData[j.id].title)}"
          onchange="onBuildField('${j.id}','title',this.value)"
          style="min-width:unset;width:150px;font-size:12px;padding:5px 7px"></td>
      <td><input type="text" data-bd-out="${j.id}" value="${esc(buildData[j.id].output)}"
          onchange="onBuildField('${j.id}','output',this.value)"
          style="min-width:unset;width:180px;font-size:12px;padding:5px 7px"></td>
      <td style="white-space:nowrap">${stHtml}</td>
      <td style="min-width:120px">${ytHtml}</td>
      <td style="min-width:90px">${bandHtml}</td>
    </tr>`;
  }

  el.innerHTML = `<div class="card" style="padding:0;overflow:hidden">
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>영상</th><th style="text-align:center">구간</th>
        <th>제목(워터마크)</th><th>출력 파일명</th><th>생성</th>
        <th>YouTube</th><th>BAND</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div></div>`;

  // 파이프라인 진행 요약 + BAND 게시 카드
  updatePipelineSummary(jobs);
  updateBandCard();
}

function onBuildField(jid, key, val) {
  if (!buildData[jid]) buildData[jid] = {};
  buildData[jid][key] = val;
  persistBuildData();
}

function updatePipelineSummary(jobs) {
  const el = document.getElementById("pipeline-summary");
  if (!el) return;
  if (!jobs.length) { el.style.display = "none"; return; }
  el.style.display = "flex";

  const builtDone = jobs.filter(j => j.status === "done" && j.output).length;
  const building  = jobs.filter(j => j.status === "building").length;
  const ytDone    = jobs.filter(j => j.yt_status === "done").length;
  const ytUp      = jobs.filter(j => j.yt_status === "uploading").length;
  const ytErr     = jobs.filter(j => j.yt_status === "error").length;
  const bandDone  = jobs.filter(j => j.band_status === "done").length;

  const parts = [];
  parts.push(`<span>생성 <b class="qs-done">${builtDone}</b>/${jobs.length}` +
             (building ? ` <span class="qs-proc">(진행중 ${building})</span>` : "") + `</span>`);
  if (ytAuth) {
    parts.push(`<span>YouTube 업로드 <b class="qs-done">${ytDone}</b>/${jobs.length}` +
               (ytUp  ? ` <span class="qs-proc">(업로드중 ${ytUp})</span>` : "") +
               (ytErr ? ` <span style="color:var(--red)">(오류 ${ytErr})</span>` : "") + `</span>`);
  }
  if (bandAuth) {
    parts.push(`<span>BAND 게시 <b class="qs-done">${bandDone}</b>/${ytDone || 0}</span>`);
  }
  el.innerHTML = parts.join("");
}

async function doBuildAll() {
  document.querySelectorAll("[data-bd-title]").forEach(i => {
    if (!buildData[i.dataset.bdTitle]) buildData[i.dataset.bdTitle] = {};
    buildData[i.dataset.bdTitle].title = i.value;
  });
  document.querySelectorAll("[data-bd-out]").forEach(i => {
    if (!buildData[i.dataset.bdOut]) buildData[i.dataset.bdOut] = {};
    buildData[i.dataset.bdOut].output = i.value;
  });
  persistBuildData();
  const quality = document.getElementById("quality").value;
  const titles = {}, outputs = {};
  allJobs.forEach(j => {
    if (buildData[j.id]) {
      titles[j.id]  = buildData[j.id].title  || "";
      outputs[j.id] = buildData[j.id].output || "";
    }
  });
  const n = allJobs.filter(j => j.n_approved !== null && j.n_approved > 0).length;
  logLine(`일괄 영상 생성 시작: ${n}개 (품질: ${quality})`, "start");
  const btn = document.getElementById("btn-build-all");
  btn.disabled = true;
  try {
    const d = await post("/api/jobs/build-all", {quality, titles, outputs});
    if (d.error) throw new Error(d.error);
    logLine(`${d.n}개 영상 생성 진행 중…`, "done");
    (d.skipped || []).forEach(n =>
      logLine(`⚠ ${n}: 채택 구간 0개 — 생성 제외 (리뷰에서 구간 선택 필요)`, "fail"));
  } catch(e) {
    logLine("생성 오류: " + e.message, "fail");
  } finally {
    setTimeout(() => { btn.disabled = false; lastBuildHash = ""; }, 3000);
  }
}

function onAutoUploadToggle(on) {
  // 세션 동안만 유지되고 영속화하지 않는다 (state.js의 restoreState 참고).
  autoUpload = on;
  if (on) logLine("생성 완료 시 자동 YouTube 업로드 켜짐 (이번 세션에서만 유지됩니다)", "done");
}

// ─── YouTube 함수 ────────────────────────────────────────
// 영상별로 구분되는 YouTube 제목 생성: "베이스 | 영상명 | 날짜"
function ytTitleFor(jid, baseTitle) {
  const j = allJobs.find(x => x.id === jid);
  const vname = j ? j.video_name.replace(/\.[^.]+$/, "") : "";
  const date  = (j && j.match_date) || localDateStr();
  const base  = (baseTitle || "").trim();
  // 해시처럼 생긴 파일명(32자 이상 16진수)은 제목에서 제외하고 날짜별 번호로 대체
  const isHash = /^[0-9a-f]{24,}$/i.test(vname);
  const parts = [base];
  if (!isHash && vname && !base.includes(vname)) parts.push(vname);
  parts.push(date);
  // 같은 날짜 잡이 여러 개면 번호 추가
  if (j) {
    const sameDateJobs = allJobs.filter(x => (x.match_date || "") === (j.match_date || "")).sort((a, b) => a.id < b.id ? -1 : 1);
    if (sameDateJobs.length > 1) parts.push(String(sameDateJobs.findIndex(x => x.id === j.id) + 1));
  }
  return parts.filter(Boolean).join(" | ").slice(0, 100);
}

async function doUploadOne(jid) {
  if (!ytAuth) { alert("YouTube 인증이 필요합니다. 인증 버튼을 클릭하세요."); return; }
  // 현재 입력된 제목(워터마크 베이스) 수집
  const titleEl = document.querySelector(`[data-bd-title="${jid}"]`);
  const base    = titleEl ? titleEl.value : (buildData[jid]?.title || DEFAULT_TITLE);
  const title   = ytTitleFor(jid, base);
  try {
    logLine(`YouTube 업로드 시작: ${title}`, "start");
    await post(`/api/jobs/${jid}/upload-youtube`, {title});
    lastBuildHash = "";
  } catch(e) { logLine(`업로드 오류: ${e.message}`, "fail"); }
}

async function doUploadAll() {
  if (!ytAuth) { alert("YouTube 인증이 필요합니다."); return; }
  const done = allJobs.filter(j => j.status === "done" && j.output &&
    j.yt_status !== "uploading" && j.yt_status !== "done");
  if (!done.length) { logLine("업로드할 완료 영상 없음", "fail"); return; }
  if (!confirm(`완료된 ${done.length}개 영상을 YouTube에 업로드합니다.`)) return;
  // 제목 수집
  document.querySelectorAll("[data-bd-title]").forEach(i => {
    if (!buildData[i.dataset.bdTitle]) buildData[i.dataset.bdTitle] = {};
    buildData[i.dataset.bdTitle].title = i.value;
  });
  persistBuildData();
  const titles = {};
  done.forEach(j => {
    const base = buildData[j.id]?.title || DEFAULT_TITLE;
    titles[j.id] = ytTitleFor(j.id, base);
  });
  try {
    const d = await post("/api/jobs/upload-all-youtube", {titles});
    logLine(`YouTube 전체 업로드 시작: ${d.n}개`, "done");
    lastBuildHash = "";
  } catch(e) { logLine(`업로드 오류: ${e.message}`, "fail"); }
}

// 생성 완료된 영상을 자동으로 YouTube 업로드 (옵션 켜진 경우)
function checkAutoUpload() {
  if (!autoUpload || !ytAuth) return;
  for (const j of allJobs) {
    if (j.status === "done" && j.output && !j.yt_status && !autoUploaded.has(j.id)) {
      autoUploaded.add(j.id);
      const base  = buildData[j.id]?.title || DEFAULT_TITLE;
      const title = ytTitleFor(j.id, base);
      logLine(`자동 업로드: ${title}`, "start");
      post(`/api/jobs/${j.id}/upload-youtube`, {title})
        .catch(e => logLine(`자동 업로드 실패: ${e.message}`, "fail"));
    }
  }
}

async function revokeYT() {
  if (!confirm("YouTube 인증을 해제합니다.")) return;
  await post("/auth/youtube/revoke", {});
  ytAuth = false;
  document.getElementById("yt-auth-badge").textContent = "미인증";
  document.getElementById("yt-auth-badge").className = "st st-error";
  lastBuildHash = "";
  logLine("YouTube 로그아웃", "done");
}

// ─── BAND 함수 ───────────────────────────────────────────
async function loadBands() {
  try {
    const d = await (await fetch("/api/auth/band/status")).json();
    bandAuth = d.ok;
    bandList = d.bands || [];
    bandListLoaded = true;
    const sel = document.getElementById("band-sel");
    if (!sel) return;
    sel.innerHTML = '<option value="">-- 선택 --</option>';
    bandList.forEach(b => {
      sel.innerHTML += `<option value="${esc(b.band_key)}">${esc(b.name)} (${b.member_count}명)</option>`;
    });
    // .env의 기본 밴드 자동 선택은 서버에서 처리
  } catch(e) {}
}

function updateBandCard() {
  const hasUploaded = allJobs.some(j => j.yt_status === "done" && j.yt_url);
  const bc = document.getElementById("band-post-card");
  if (!bc) return;
  // YouTube 업로드 완료된 것이 있으면 항상 표시 (BAND 인증 불필요)
  bc.style.display = hasUploaded ? "block" : "none";
  if (!hasUploaded) return;

  updateBandPreview();

  // BAND API 직접 게시 섹션: 인증된 경우만 표시
  const apiSec = document.getElementById("band-api-section");
  if (apiSec) apiSec.style.display = bandAuth ? "block" : "none";
  if (bandAuth && !bandListLoaded) loadBands();
}

function updateBandPreview() {
  const prev = document.getElementById("band-post-preview");
  if (!prev) return;
  const uploaded = allJobs.filter(j => j.yt_status === "done" && j.yt_url
    && j.band_status !== "done");
  if (!uploaded.length) { prev.textContent = "게시할 새 링크 없음"; return; }

  // 날짜별 그룹핑 미리보기
  const groups = {};
  const today = localDateStr();
  uploaded.forEach(j => {
    const d = j.match_date || today;
    if (!groups[d]) groups[d] = [];
    groups[d].push([j.video_name.replace(/\.[^.]+$/,""), j.yt_url]);
  });
  let text = "";
  for (const [d, pairs] of Object.entries(groups)) {
    const dt = new Date(d + "T00:00:00");
    const ds = `${dt.getFullYear()}년 ${dt.getMonth()+1}월 ${dt.getDate()}일`;
    text += `${ds} 경기 하이라이트 영상입니다.\n\n`;
    pairs.forEach(([n, u]) => { text += `· ${n}\n${u}\n\n`; });
    text += "HLEditor 자동 생성\n\n---\n";
  }
  prev.textContent = text.trim();
}

async function copyBandText(silent = false) {
  const prev = document.getElementById("band-post-preview");
  const text = prev?.textContent?.trim();
  if (!text || text === "게시할 새 링크 없음") {
    if (!silent) alert("복사할 링크가 없습니다. YouTube 업로드를 먼저 완료하세요."); return;
  }
  const resultEl = document.getElementById("band-copy-result");
  try {
    await navigator.clipboard.writeText(text);
    resultEl.innerHTML = '<span style="color:var(--accent);font-weight:600">복사 완료!</span> <span class="hint">BAND 앱에서 새 게시글 작성 후 붙여넣기 하세요.</span>';
    const btn = document.getElementById("btn-copy-band");
    if (btn) { const orig = btn.textContent; btn.textContent = "복사됨"; setTimeout(() => { btn.textContent = orig; }, 2000); }
  } catch(e) {
    // 클립보드 API 불가 시 텍스트 선택
    const range = document.createRange();
    range.selectNodeContents(prev);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    resultEl.innerHTML = '<span class="hint">텍스트가 선택되었습니다. Ctrl+C로 복사하세요.</span>';
  }
  setTimeout(() => { if (resultEl) resultEl.innerHTML = ""; }, 5000);
}

async function doPostBand() {
  if (!bandAuth) { alert("BAND 인증이 필요합니다."); return; }
  const sel = document.getElementById("band-sel");
  const band_key = sel ? sel.value : "";
  if (!band_key) { alert("밴드를 선택하세요."); return; }

  const resultEl = document.getElementById("band-post-result");
  if (resultEl) resultEl.innerHTML = '<span class="spin"></span><span class="hint">게시 중…</span>';
  try {
    const d = await post("/api/jobs/post-band", {band_key});
    const n = (d.posted||[]).reduce((s,p) => s + p.n, 0);
    logLine(`BAND 게시 완료: ${n}개 영상 링크`, "done");
    if (resultEl) resultEl.innerHTML =
      (d.posted||[]).map(p =>
        `<span style="color:var(--accent);font-size:12px">✓ ${p.date} ${p.n}개 게시됨</span>`+
        (p.url ? ` <a href="${esc(p.url)}" target="_blank" style="color:var(--blue);font-size:11px">↗</a>` : "")
      ).join("<br>");
    (d.errors||[]).forEach(e => logLine(`BAND 오류 ${e.date}: ${e.error}`, "fail"));
    lastBuildHash = "";
    sendNotif("BAND 게시 완료", `${n}개 영상 링크가 BAND에 게시되었습니다.`);
  } catch(e) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--red);font-size:12px">오류: ${esc(e.message)}</span>`;
    logLine("BAND 게시 실패: " + e.message, "fail");
  }
}

async function revokeBAND() {
  if (!confirm("BAND 인증을 해제합니다.")) return;
  await post("/auth/band/revoke", {});
  bandAuth = false;
  const bc = document.getElementById("band-post-card");
  if (bc) bc.style.display = "none";
  document.getElementById("band-auth-badge").textContent = "미인증";
  document.getElementById("band-auth-badge").className = "st st-error";
  logLine("BAND 로그아웃", "done");
}
