// state.js — 전역 상태 · localStorage 영속화
// window.HL_CONFIG(Jinja 주입)에서 값을 받아 이후 로드되는 모든 static/js/*.js가
// 참조할 수 있는 전역 바인딩으로 만든다. (classic <script> 는 전역 렉시컬 스코프를
// 공유하므로 이후 로드되는 파일에서 CONF_AUTO 등을 그대로 참조할 수 있다.)
const CONF_AUTO      = window.HL_CONFIG.CONF_AUTO;
const CONF_MAYBE     = window.HL_CONFIG.CONF_MAYBE;
const SENSITIVITIES  = window.HL_CONFIG.SENSITIVITIES;
const YT_AUTH        = window.HL_CONFIG.YT_AUTH;
const BAND_AUTH      = window.HL_CONFIG.BAND_AUTH;
const DEFAULT_TITLE  = window.HL_CONFIG.DEFAULT_TITLE;

// ─── 전역 상태 ──────────────────────────────────────────
let allJobs    = [];
let jobCands   = {};     // jid → {cands, visionUsed}
let localSel   = {};     // jid → Set of indices
let savedSel   = {};     // jid → array (서버 저장 완료)
let expanded   = {};     // jid → bool
let confVal    = {};     // jid → float
let buildData  = {};     // jid → {title, output}
let activePne  = "queue";
let logStarted = false;
let staged     = [];     // [{path, basename, sensitivity}]
let pathsTimer = null;
let globalConf = CONF_AUTO;

// 알림 상태 추적
let prevStatuses   = {};
let wasProcessing  = false;

// 렌더 최적화 (해시 캐싱)
let lastQueueHash  = "";
let lastReviewHash = "";
let lastBuildHash  = "";

// YouTube / BAND 상태
let ytAuth   = YT_AUTH;
let bandAuth = BAND_AUTH;
let bandList = [];   // [{band_key, name, member_count}]
let bandListLoaded = false;
let autoUpload   = false;
let autoUploaded = new Set();   // 자동 업로드 트리거한 jid 추적

// ─── 원스톱 파이프라인 상태 ──────────────────────────────
// _pipe: null | { mode:"youtube"|"band", jobIds:Set, step:string, stepAt:number }
// step: "processing" → "building" → "uploading" → "posting" → "done"
let _pipe = null;
const PIPE_STEP_DELAY = 3000; // 단계 전환 후 안정화 대기 (ms)

// ─── localStorage 영속화 ─────────────────────────────────
const LS = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("hl_" + k)) ?? d; } catch(e) { return d; } },
  set(k, v) { try { localStorage.setItem("hl_" + k, JSON.stringify(v)); } catch(e) {} },
};
function persistBuildData() { LS.set("buildData", buildData); }

function _savePipe() {
  if (!_pipe) { LS.set("pipe", null); return; }
  LS.set("pipe", { mode: _pipe.mode, jobIds: [..._pipe.jobIds], step: _pipe.step, stepAt: _pipe.stepAt });
}

function restoreState() {
  buildData  = LS.get("buildData", {}) || {};
  globalConf = LS.get("globalConf", CONF_AUTO);
  // autoUpload는 의도적으로 영속화하지 않는다 — 새로고침/재시작 후에도 켜진 채로
  // 남아 있으면 사용자가 모르는 사이 실제 YouTube 채널에 업로드가 시도될 수 있다.
  // 매 세션 기본 꺼짐으로 시작해 항상 명시적으로 켜도록 한다.
  autoUpload = false;
  const saved = LS.get("pipe", null);
  if (saved && saved.jobIds?.length && saved.step && saved.step !== "done") {
    _pipe = { mode: saved.mode, jobIds: new Set(saved.jobIds), step: saved.step, stepAt: saved.stepAt || Date.now() };
  }
}

// DOM 준비 후 복원값 반영
function applyRestoredUI() {
  const gc = document.getElementById("global-conf");
  const gv = document.getElementById("gcv");
  if (gc) gc.value = globalConf;
  if (gv) gv.textContent = Number(globalConf).toFixed(2);
  const au = document.getElementById("auto-upload");
  const na = document.getElementById("auto-upload-na");
  if (au) {
    au.checked = autoUpload && ytAuth;
    au.disabled = !ytAuth;
    if (!ytAuth && na) na.textContent = "(YouTube 인증 후 사용 가능)";
  }
}
