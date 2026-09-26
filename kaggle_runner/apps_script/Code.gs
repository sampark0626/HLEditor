/**
 * HLEditor 인박스 트리거 — Google Apps Script
 *
 * Drive의 HLEditor/01_inbox(·01_inbox_2d)에 영상이 있으면 Kaggle 커널을 실행(push)한다.
 * 설계: MOBILE_PLAN.md 3-3 · 설치: kaggle_runner/INBOX_SETUP.md
 *
 * 스크립트 속성 (프로젝트 설정 → 스크립트 속성):
 *   필수  KAGGLE_USERNAME, KAGGLE_API_TOKEN, ROOT_FOLDER_ID, NTFY_TOPIC
 *   선택  KERNEL_SLUG (기본 hleditor-inbox), SECRETS_DATASET (기본 hl-secrets),
 *         FAST_DAYS (기본 "7,1" — 1=월 … 7=일, 이 요일엔 5분마다·나머지는 1시간마다 확인),
 *         KICK_KEY (실행 링크 doGet을 배포할 때만)
 *
 * 처음 한 번 installTrigger()를 실행하면 5분 간격 트리거가 걸린다.
 * 이 파일은 node kaggle_runner/apps_script/code_test.js 로 가짜 환경에서 시험한다.
 */

var KAGGLE_API = 'https://api.kaggle.com/v1/kernels.KernelsApiService/';
var NTFY_URL = 'https://ntfy.sh/';
var BOOTSTRAP_URL = 'https://raw.githubusercontent.com/sampark0626/HLEditor/main/kaggle_runner/kernel_bootstrap.py';
var INBOX = '01_inbox';
var INBOX_2D = '01_inbox_2d';
var VIDEO_EXT = /\.(mp4|mov|m4v|mkv|avi)$/i;
var BUSY_STATES = ['QUEUED', 'RUNNING', 'NEW_SCRIPT', 'CANCEL_REQUESTED'];
var COOLDOWN_MIN = 10;        // push 직후엔 상태가 늦게 바뀔 수 있어 이 시간 동안 다시 push하지 않는다
var SLOW_INTERVAL_MIN = 55;   // FAST_DAYS가 아닌 요일의 확인 간격(5분 트리거 중 약 1시간에 한 번)
var BREAKER_LIMIT = 3;        // 연속 실패 이만큼이면 자동 실행을 멈춘다

// raw GitHub에서 받지 못할 때 쓰는 kernel_bootstrap.py 사본 — 저장소 파일과 같게 유지한다
var BOOTSTRAP_FALLBACK = [
  'import subprocess',
  'import sys',
  '',
  'REPO_URL = "https://github.com/sampark0626/HLEditor.git"',
  'REPO_DIR = "/tmp/HLEditor"',
  '',
  'subprocess.run(["rm", "-rf", REPO_DIR], check=False)',
  'subprocess.run(["git", "clone", "--depth", "1", REPO_URL, REPO_DIR], check=True)',
  'sys.path.insert(0, REPO_DIR)',
  '',
  'from kaggle_runner import inbox_runner  # noqa: E402',
  '',
  'sys.exit(inbox_runner.main([]))',
  ''
].join('\n');

/** 시간 트리거와 실행 링크가 부르는 진입점. 결과는 실행 기록(로그)에 남기고 문자열로 돌려준다. */
function tick(opts) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return log_('다른 확인이 진행 중입니다');
  try {
    return log_(tick_(!!(opts && opts.force === true)));
  } catch (e) {
    console.error(e);
    return '오류: ' + (e && e.message ? e.message : e);
  } finally {
    lock.releaseLock();
  }
}

/** 편집기에서 함수를 실행하면 반환값은 안 보이고 로그만 보이므로, 결과를 로그에도 남긴다. */
function log_(msg) {
  console.log(msg);
  return msg;
}

function tick_(force) {
  var props = PropertiesService.getScriptProperties();
  var cfg = readConfig_(props);
  var now = new Date();

  if (!force && !isFastDay_(now, cfg.fastDays)) {
    var lastSlow = Number(props.getProperty('LAST_SLOW_CHECK_AT') || 0);
    if (now.getTime() - lastSlow < SLOW_INTERVAL_MIN * 60000) return '건너뜀: 확인 요일이 아님';
    props.setProperty('LAST_SLOW_CHECK_AT', String(now.getTime()));
  }

  var counts = countInbox_(cfg.rootFolderId);
  var total = counts.inbox + counts.inbox2d;
  var lastPush = props.getProperty('LAST_PUSH_AT');
  var outcomePending = !!lastPush && props.getProperty('OUTCOME_FOR') !== lastPush;
  if (total === 0 && !outcomePending) return '새 영상 없음';

  var status = kernelStatus_(cfg);
  recordOutcome_(props, status);
  if (total === 0) return '새 영상 없음';
  if (status === 'BUSY') return '처리 중 — 영상 ' + total + '개는 실행 중인 커널이 이어서 가져갑니다';
  if (lastPush && now.getTime() - Number(lastPush) < COOLDOWN_MIN * 60000) {
    return '방금 실행을 요청했습니다 — 잠시 뒤 다시 확인합니다';
  }
  if (Number(props.getProperty('ERROR_STREAK') || 0) >= BREAKER_LIMIT) {
    notifyBreakerOnce_(props, cfg);
    return '연속 실패로 자동 실행이 멈춰 있습니다 — Kaggle 로그 확인 후 resetBreaker() 실행';
  }

  var useGpu = counts.inbox2d > 0;
  pushKernel_(cfg, useGpu);
  props.setProperty('LAST_PUSH_AT', String(now.getTime()));
  notify_(cfg, '영상 ' + total + '개 확인 — 처리 시작',
          '경기당 약 25분 뒤 완료 알림이 옵니다.' +
          (useGpu ? '\n2D 변환 요청이 있어 GPU로 실행합니다.' : ''), ['soccer']);
  return '영상 ' + total + '개 발견 — Kaggle 실행 요청함' + (useGpu ? ' (GPU)' : '');
}

/** 실행 링크: 웹 앱으로 배포(실행 계정: 나, 접근: 모든 사용자)하고 ?key=KICK_KEY 로 부른다. */
function doGet(e) {
  var key = PropertiesService.getScriptProperties().getProperty('KICK_KEY');
  var given = (e && e.parameter && e.parameter.key) || '';
  var msg = (!key || given !== key) ? '거부됨: key가 맞지 않습니다' : tick({force: true});
  return ContentService.createTextOutput(msg);
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'tick') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('tick').timeBased().everyMinutes(5).create();
  return log_('5분 간격 트리거를 설치했습니다');
}

function resetBreaker() {
  var props = PropertiesService.getScriptProperties();
  props.setProperty('ERROR_STREAK', '0');
  props.deleteProperty('BREAKER_NOTIFIED');
  return log_('자동 실행을 다시 켰습니다');
}

/** 편집기에서 수동 확인용: Kaggle 연결과 커널 상태 (처음엔 커널이 없어서 IDLE이 정상) */
function showStatus() {
  return log_('Kaggle 커널 상태: ' + kernelStatus_(readConfig_(PropertiesService.getScriptProperties())));
}

/** 편집기에서 수동 확인용: 인박스와 상관없이 CPU로 한 번 실행 — 영상이 없어도 연결 전체를 시험한다 */
function testPush() {
  var res = pushKernel_(readConfig_(PropertiesService.getScriptProperties()), false);
  return log_('Kaggle 실행 요청 성공: ' + JSON.stringify(res) +
              '\nkaggle.com → Code → hleditor-inbox 에서 2~5분 뒤 로그를 확인하세요.');
}

// ─── 내부 함수 ───────────────────────────────────────────────────────────
function readConfig_(props) {
  var p = props.getProperties();
  var missing = ['KAGGLE_USERNAME', 'KAGGLE_API_TOKEN', 'ROOT_FOLDER_ID', 'NTFY_TOPIC']
    .filter(function (k) { return !p[k]; });
  if (missing.length) throw new Error('스크립트 속성 누락: ' + missing.join(', '));
  return {
    user: p.KAGGLE_USERNAME,
    token: p.KAGGLE_API_TOKEN,
    rootFolderId: p.ROOT_FOLDER_ID,
    topic: p.NTFY_TOPIC,
    slug: p.KERNEL_SLUG || 'hleditor-inbox',
    secretsDataset: p.SECRETS_DATASET || 'hl-secrets',
    fastDays: String(p.FAST_DAYS || '7,1').split(',')
      .map(function (s) { return Number(s.trim()); })
      .filter(function (n) { return n >= 1 && n <= 7; })
  };
}

function isFastDay_(now, fastDays) {
  var day = Number(Utilities.formatDate(now, 'Asia/Seoul', 'u'));  // 1=월 … 7=일
  return fastDays.indexOf(day) >= 0;
}

function countInbox_(rootId) {
  var root = DriveApp.getFolderById(rootId);
  return {inbox: countVideos_(root, INBOX), inbox2d: countVideos_(root, INBOX_2D)};
}

function countVideos_(root, name) {
  var folders = root.getFoldersByName(name);
  if (!folders.hasNext()) return 0;
  var files = folders.next().getFiles();
  var n = 0;
  while (files.hasNext()) {
    var f = files.next();
    if (f.isTrashed()) continue;
    if (String(f.getMimeType()).indexOf('video/') === 0 || VIDEO_EXT.test(f.getName())) n++;
  }
  return n;
}

/** 새 API 토큰(KGAT_…)은 Bearer, 예전 kaggle.json의 key는 아이디와 함께 Basic 인증으로 보낸다. */
function authHeader_(cfg) {
  if (cfg.token.indexOf('KGAT_') === 0) return 'Bearer ' + cfg.token;
  return 'Basic ' + Utilities.base64Encode(cfg.user + ':' + cfg.token);
}

function kaggle_(cfg, method, body) {
  var resp = UrlFetchApp.fetch(KAGGLE_API + method, {
    method: 'post',
    contentType: 'application/json',
    headers: {Authorization: authHeader_(cfg)},
    payload: JSON.stringify(body),
    muteHttpExceptions: true
  });
  var text = resp.getContentText();
  var json = null;
  try { json = JSON.parse(text); } catch (e) { json = null; }
  return {code: resp.getResponseCode(), text: text, json: json};
}

/** 'BUSY' | 'ERROR' | 'COMPLETE' | 'IDLE'(커널이 아직 없거나 취소됨) */
function kernelStatus_(cfg) {
  var res = kaggle_(cfg, 'GetKernelSessionStatus', {userName: cfg.user, kernelSlug: cfg.slug});
  // 아직 없는 커널을 조회하면 Kaggle은 404가 아니라 403 "Permission 'kernels.get' was denied"로
  // 답한다(2026-09-26 실측). 토큰이 틀린 경우는 이어지는 push가 자기 오류로 드러낸다.
  if (res.code === 404 || (res.code === 403 && /kernels\.get/.test(res.text))) return 'IDLE';
  if (res.code !== 200) {
    throw new Error('Kaggle 상태 조회 실패 (HTTP ' + res.code + '): ' + String(res.text).slice(0, 200));
  }
  var s = String((res.json && res.json.status) || '').toUpperCase();
  if (BUSY_STATES.some(function (b) { return s.indexOf(b) >= 0; })) return 'BUSY';
  if (s.indexOf('ERROR') >= 0) return 'ERROR';
  if (s.indexOf('COMPLETE') >= 0) return 'COMPLETE';
  return 'IDLE';
}

/** push 이후 처음 확인한 결과만 센다 — 같은 실행을 여러 번 세지 않게 */
function recordOutcome_(props, status) {
  var lastPush = props.getProperty('LAST_PUSH_AT');
  if (!lastPush || props.getProperty('OUTCOME_FOR') === lastPush) return;
  if (status === 'ERROR') {
    props.setProperty('ERROR_STREAK', String(Number(props.getProperty('ERROR_STREAK') || 0) + 1));
    props.setProperty('OUTCOME_FOR', lastPush);
  } else if (status === 'COMPLETE') {
    props.setProperty('ERROR_STREAK', '0');
    props.deleteProperty('BREAKER_NOTIFIED');
    props.setProperty('OUTCOME_FOR', lastPush);
  }
}

function pushKernel_(cfg, useGpu) {
  var body = {
    slug: cfg.user + '/' + cfg.slug,
    newTitle: cfg.slug,
    text: bootstrapText_(),
    language: 'python',
    kernelType: 'script',
    isPrivate: true,
    enableInternet: true,
    enableGpu: useGpu,
    datasetDataSources: [cfg.user + '/' + cfg.secretsDataset]
  };
  if (useGpu) body.machineShape = 'NvidiaTeslaT4';
  var res = kaggle_(cfg, 'SaveKernel', body);
  var j = res.json || {};
  var invalid = ['invalidTags', 'invalidDatasetSources', 'invalidCompetitionSources',
                 'invalidKernelSources', 'invalidModelSources']
    .filter(function (k) { return j[k] && j[k].length; });
  if (res.code !== 200 || j.error || invalid.length) {
    throw new Error('Kaggle 실행 요청 실패 (HTTP ' + res.code + ') ' + (j.error || '') + ' ' +
      invalid.map(function (k) { return k + '=' + JSON.stringify(j[k]); }).join(' ') +
      (res.json ? '' : String(res.text).slice(0, 200)));
  }
  return j;
}

function bootstrapText_() {
  try {
    var r = UrlFetchApp.fetch(BOOTSTRAP_URL, {muteHttpExceptions: true});
    var text = r.getContentText();
    if (r.getResponseCode() === 200 && text.indexOf('inbox_runner') >= 0) return text;
  } catch (e) {
    console.warn('kernel_bootstrap.py를 받지 못해 내장 사본을 씁니다: ' + e);
  }
  return BOOTSTRAP_FALLBACK;
}

function notify_(cfg, title, message, tags) {
  try {
    UrlFetchApp.fetch(NTFY_URL, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({topic: cfg.topic, title: title, message: message, tags: tags || []}),
      muteHttpExceptions: true
    });
  } catch (e) {
    console.warn('ntfy 알림 실패: ' + e);
  }
}

function notifyBreakerOnce_(props, cfg) {
  if (props.getProperty('BREAKER_NOTIFIED')) return;
  notify_(cfg, 'Kaggle 자동 실행 중지',
          'Kaggle 실행이 ' + BREAKER_LIMIT + '번 연속 실패해 자동 실행을 멈췄습니다. ' +
          'Kaggle 노트북 hleditor-inbox의 로그를 확인한 뒤 Apps Script에서 resetBreaker()를 실행해 주세요.',
          ['warning']);
  props.setProperty('BREAKER_NOTIFIED', '1');
}
