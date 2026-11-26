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
 *         NOTIFY_START (1이면 Kaggle을 시작할 때 "처리 시작" 알림도 보낸다. 기본 꺼짐 —
 *         영상을 여러 개 올리는 중에 일찍 오면 다 올라간 줄 알고 헷갈리기 때문),
 *         KICK_KEY (실행 링크 doGet을 배포할 때만),
 *         REVIEW_URL (검토 페이지 웹 앱 주소 — "접근: 나만"으로 배포한 것. 자동 게시 알림을 누르면 열린다)
 *
 * 처음 한 번 installTrigger()를 실행하면 5분 간격 트리거가 걸린다.
 * 검토 페이지(Review.html)를 쓰려면 installDailyTrigger()도 한 번 실행한다(매일 09:00 자동 게시).
 * 검토 페이지·자동 게시·월간 설계: MONTHLY_PLAN.md Phase 2. 설치: kaggle_runner/INBOX_SETUP.md
 * 이 파일은 node kaggle_runner/apps_script/code_test.js 로 가짜 환경에서 시험한다.
 */

var KAGGLE_API = 'https://api.kaggle.com/v1/kernels.KernelsApiService/';
var NTFY_URL = 'https://ntfy.sh/';
var BOOTSTRAP_URL = 'https://raw.githubusercontent.com/sampark0626/HLEditor/main/kaggle_runner/kernel_bootstrap.py';
var INBOX = '01_inbox';
var INBOX_2D = '01_inbox_2d';
var VIDEO_EXT = /\.(mp4|mov|m4v|mkv|avi)$/i;
var BUSY_STATES = ['QUEUED', 'RUNNING', 'NEW_SCRIPT', 'CANCEL_REQUESTED'];
var TZ = 'Asia/Seoul';
var COOLDOWN_MIN = 10;       // push 직후엔 상태가 늦게 바뀔 수 있어 이 시간 동안 다시 push하지 않는다
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
  // 검토 페이지의 게시·제목 변경 요청도 일거리다 — 인박스가 비어 있어도 커널을 띄워야 게시가 나간다(M10)
  var requests = countPendingRequests_(cfg.rootFolderId);
  var work = total + requests;
  var lastPush = props.getProperty('LAST_PUSH_AT');
  var outcomePending = !!lastPush && props.getProperty('OUTCOME_FOR') !== lastPush;
  if (work === 0 && !outcomePending) return '새 영상 없음';

  var status = kernelStatus_(cfg);
  recordOutcome_(props, status);
  if (work === 0) return '새 영상 없음';
  if (status === 'BUSY') return '처리 중 — ' + describeWork_(total, requests) + '는 실행 중인 커널이 이어서 가져갑니다';
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
  // 업로드가 아직 진행 중일 수 있어 "영상 N개 확인"은 기본으로 보내지 않는다 — 알림은 처리가
  // 다 끝난 뒤 러너가 한 통만 보낸다. 필요하면 NOTIFY_START=1.
  if (cfg.notifyStart && total > 0) {
    notify_(cfg, '영상 ' + total + '개 확인 — 처리 시작',
            '올린 영상을 모두 처리한 뒤 완료 알림이 한 번 옵니다.' +
            (useGpu ? '\n2D 변환 요청이 있어 GPU로 실행합니다.' : ''), ['soccer']);
  }
  return describeWork_(total, requests) + ' 발견 — Kaggle 실행 요청함' + (useGpu ? ' (GPU)' : '');
}

/** "영상 2개" / "게시 요청 1건" / "영상 2개·게시 요청 1건" */
function describeWork_(total, requests) {
  var parts = [];
  if (total > 0 || requests === 0) parts.push('영상 ' + total + '개');
  if (requests > 0) parts.push('게시 요청 ' + requests + '건');
  return parts.join('·');
}

/**
 * 웹 앱 진입점 두 가지:
 *  - 실행 링크: 웹 앱으로 배포(실행 계정: 나, 접근: 모든 사용자)하고 ?key=KICK_KEY 로 부른다.
 *  - 검토 페이지: ?page=review[&date=YYYY-MM-DD]. 소유자 본인만 연다 — "접근: 나만"으로 따로 배포한 주소로.
 *    "모든 사용자" 배포에서는 로그인 정보가 없어 소유자 확인에서 거부된다.
 */
function doGet(e) {
  var params = (e && e.parameter) || {};
  if (params.page === 'review') return reviewPage_(params);
  var key = PropertiesService.getScriptProperties().getProperty('KICK_KEY');
  var given = params.key || '';
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

/** 매일 09:00(한국 시간)에 dailyJobs를 부르는 트리거. 5분 tick 트리거와는 따로 관리한다(M15). */
function installDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'dailyJobs') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('dailyJobs').timeBased().everyDays(1).atHour(9).nearMinute(0)
    .inTimezone(TZ).create();
  return log_('매일 09:00(' + TZ + ') 트리거를 설치했습니다');
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
    notifyStart: /^(1|true|yes|on)$/i.test(String(p.NOTIFY_START || '').trim()),
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

/** click이 있으면 알림을 눌렀을 때 그 주소가 열린다(없으면 ntfy 앱이 본문을 복사한다 — M8). */
function notify_(cfg, title, message, tags, click) {
  try {
    var payload = {topic: cfg.topic, title: title, message: message, tags: tags || []};
    if (click) payload.click = click;
    UrlFetchApp.fetch(NTFY_URL, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
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

// ═══ 검토 페이지 · 자동 게시 (MONTHLY_PLAN.md Phase 2) ═══════════════════════════
// 러너(Python)와 이 파일이 Drive `_state/`의 JSON으로 대화한다. 누가 무엇을 쓰는지는 상태로 나눈다(3-4):
//   pending → publish        이 파일(검토 페이지의 "게시" / dailyJobs의 자동 게시) — 대기열 requests.json은 이 파일만 쓴다
//   publish → running → published·auto_published·failed   러너(publish.py)
//   pending·게시 뒤의 태그(kind·opponent·attack_end·clip_tags …)   이 파일(검토 페이지)
// 규칙 표(sideOf_·monthlyEligible_·gameName_)는 kaggle_runner/tags.py·naming.py와 똑같아야 한다 —
// tests/data/tag_rules.json 하나를 pytest와 code_test.js가 같이 읽어 고정한다.

var STATE_FOLDER = '_state';
var REQUESTS_FILE = 'requests.json';
var GAMES_RE = /^games_(\d{4}-\d{2}-\d{2})_(.+)\.json$/;
var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
var MONTH_RE = /^\d{4}-\d{2}$/;
var LABEL_RE = /^\d{1,3}경기$/;
var KIND_SCRIMMAGE = 'scrimmage';
var KIND_OPPONENT = 'opponent';
var KINDS = [KIND_SCRIMMAGE, KIND_OPPONENT];
var SCRIMMAGE_LABEL = '청백전';
var ATTACK = 'attack';
var DEFENSE = 'defense';
var FLIP_END = {building: 'tree', tree: 'building'};
var EDITABLE_STATES = ['pending', 'auto_published', 'published', 'failed'];   // publish·running은 러너가 읽는 중이라 고치지 않는다
var PUBLISHABLE_STATES = ['pending', 'failed'];                                // failed는 "다시 게시"
var PUBLISH_STATES = ['publish', 'running'];
var DONE_STATES = ['published', 'auto_published'];
var MAX_RETITLE_ATTEMPTS = 3;
var SHOT_TYPES = ['goal', 'shot', 'save'];
var RECENT_DAYS = 45;          // 검토 목록에 보여 주는 기간
var AUTO_LOOKBACK_DAYS = 14;   // dailyJobs가 자동 게시 대상을 찾는 기간
var OPPONENT_MAX = 40;
var TITLE_MAX = 100;
var FILENAME_MAX = 30;
var RECENT_OPPONENTS_KEY = 'RECENT_OPPONENTS';
var RECENT_OPPONENTS_MAX = 8;

// ─── 소유자 확인 ─────────────────────────────────────────────────────────────
/** 로그인한 사용자가 있고 스크립트 소유자(웹 앱 실행 계정)와 같을 때만 true. "모든 사용자" 배포에서는 이메일이 비어 거부된다. */
function isOwner_() {
  try {
    var user = Session.getActiveUser().getEmail();
    var me = Session.getEffectiveUser().getEmail();
    return !!user && user === me;
  } catch (e) {
    return false;
  }
}

function assertOwner_() {
  if (!isOwner_()) throw new Error('거부됨: 소유자 본인만 쓸 수 있습니다');
}

function reviewPage_(params) {
  if (!isOwner_()) {
    return ContentService.createTextOutput('거부됨: 소유자만 열 수 있습니다. "접근: 나만"으로 배포한 검토 페이지 주소로 여세요.');
  }
  var tpl = HtmlService.createTemplateFromFile('Review');
  // 주소의 값은 형식이 맞을 때만 페이지 스크립트에 싣는다(그대로 넣으면 스크립트 삽입이 된다)
  tpl.initial = JSON.stringify({
    date: DATE_RE.test(params.date || '') ? params.date : '',
    month: MONTH_RE.test(params.month || '') ? params.month : ''
  });
  return tpl.evaluate().setTitle('HLEditor 검토')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ─── 규칙 (tags.py·naming.py와 같다) ─────────────────────────────────────────
function safeFilename_(text, maxLen) {
  var cleaned = String(text == null ? '' : text).replace(/[\/\\:*?"<>|\u0000-\u001f]/g, '_').trim();
  return cleaned.slice(0, maxLen == null ? FILENAME_MAX : maxLen).trim();
}

/** 경기 이름 `날짜-n경기[-상대|-청백전]`. forFile이면 상대 이름을 파일 이름용으로 다듬는다. */
function gameName_(date, label, kind, opponent, forFile) {
  var base = date + '-' + label;
  var suffix;
  if (kind === KIND_SCRIMMAGE || kind === SCRIMMAGE_LABEL) {
    suffix = SCRIMMAGE_LABEL;
  } else {
    suffix = String(opponent == null ? '' : opponent).trim();
    if (suffix && forFile) suffix = safeFilename_(suffix);
  }
  return (suffix ? base + '-' + suffix : base).slice(0, TITLE_MAX);
}

function clipTag_(game, idx) {
  var tags = game.clip_tags || {};
  return tags[String(idx)] || {};
}

function isRemoved_(game, idx) {
  return !!clipTag_(game, idx).removed;
}

/** 클립이 한울타리 공격('attack')인지 수비('defense')인지. 알 수 없으면 null. */
function sideOf_(clip, game) {
  var override = clipTag_(game, clip.idx).side_override;
  if (override === ATTACK || override === DEFENSE) return override;
  var end = clip.end, attackEnd = game.attack_end;
  if (!FLIP_END[end] || !FLIP_END[attackEnd]) return null;
  var sw = game.switch_at_idx;
  if (sw !== null && sw !== undefined && clip.idx >= sw) attackEnd = FLIP_END[attackEnd];   // "여기부터 반대"
  return end === attackEnd ? ATTACK : DEFENSE;
}

function isConceded_(clip, game) {
  return !!clipTag_(game, clip.idx).conceded && sideOf_(clip, game) === DEFENSE;
}

function isScrimmage_(game) {
  return game.kind === KIND_SCRIMMAGE || game.kind === SCRIMMAGE_LABEL;
}

/** 주간 영상 = 전부 − "잘못 잡힘". 팀·골대·실점으로는 아무것도 빼지 않는다. */
function weeklyClips_(clips, game) {
  return clips.filter(function (c) { return !isRemoved_(game, c.idx); });
}

/** 월간 대상: 청백전 전부, 다른 팀은 공격 + 실점 아닌 수비(판단 불가·태깅 안 됨은 제외). */
function monthlyEligible_(clips, game) {
  return weeklyClips_(clips, game).filter(function (c) {
    if (isScrimmage_(game)) return true;
    if (game.kind !== KIND_OPPONENT) return false;
    var side = sideOf_(c, game);
    return side === ATTACK || (side === DEFENSE && !isConceded_(c, game));
  });
}

// ─── _state 읽기·쓰기 ────────────────────────────────────────────────────────
function rootFolderId_() {
  var id = PropertiesService.getScriptProperties().getProperty('ROOT_FOLDER_ID');
  if (!id) throw new Error('스크립트 속성 누락: ROOT_FOLDER_ID');
  return id;
}

function stateFolder_(rootId) {
  var it = DriveApp.getFolderById(rootId).getFoldersByName(STATE_FOLDER);
  return it.hasNext() ? it.next() : null;
}

function requireState_() {
  var folder = stateFolder_(rootFolderId_());
  if (!folder) throw new Error('Drive HLEditor/_state 폴더가 없습니다 — 러너가 한 번도 실행되지 않았습니다');
  return folder;
}

function parseFile_(file) {
  try {
    return JSON.parse(file.getBlob().getDataAsString('UTF-8'));
  } catch (e) {
    return null;
  }
}

function readJson_(folder, name) {
  if (!folder) return null;
  var it = folder.getFilesByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (f.isTrashed()) continue;
    return parseFile_(f);
  }
  return null;
}

function writeJson_(folder, name, data) {
  var text = JSON.stringify(data);
  var it = folder.getFilesByName(name);
  while (it.hasNext()) {
    var f = it.next();
    if (f.isTrashed()) continue;
    f.setContent(text);
    return;
  }
  folder.createFile(name, text, 'application/json');
}

function labelNumber_(label) {
  return Number(String(label).replace(/\D/g, '')) || 0;
}

/** games_*.json을 날짜·경기 번호 순으로 읽는다. opts: {date, minDate} */
function listGames_(folder, opts) {
  var out = [];
  if (!folder) return out;
  var it = folder.getFiles();
  while (it.hasNext()) {
    var f = it.next();
    var m = GAMES_RE.exec(f.getName());
    if (!m || f.isTrashed()) continue;
    if (opts && opts.date && m[1] !== opts.date) continue;
    if (opts && opts.minDate && m[1] < opts.minDate) continue;
    var game = parseFile_(f);
    if (game) out.push(game);
  }
  out.sort(function (a, b) {
    return a.date < b.date ? -1 : a.date > b.date ? 1 : labelNumber_(a.label) - labelNumber_(b.label);
  });
  return out;
}

function gamesName_(date, label) { return 'games_' + date + '_' + label + '.json'; }
function clipsName_(date, label) { return 'clips_' + date + '_' + label + '.json'; }

function loadGame_(folder, date, label) {
  var game = readJson_(folder, gamesName_(date, label));
  if (!game) throw new Error('경기 기록이 없습니다: ' + date + ' ' + label);
  return game;
}

function saveGame_(folder, game) {
  writeJson_(folder, gamesName_(game.date, game.label), game);
}

function checkDate_(date) {
  if (!DATE_RE.test(String(date || ''))) throw new Error('날짜 형식이 맞지 않습니다: ' + date);
}

function checkKey_(date, label) {
  checkDate_(date);
  if (!LABEL_RE.test(String(label || ''))) throw new Error('경기 이름 형식이 맞지 않습니다: ' + label);
}

function withLock_(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

/** 러너가 읽는 ISO 형식(2026-09-27T05:00:00+00:00) — 밀리초·Z 없이 쓴다. */
function nowIso_() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00');
}

function kstDate_(ms) {
  return Utilities.formatDate(new Date(ms), TZ, 'yyyy-MM-dd');
}

function parseTime_(text) {
  var t = Date.parse(String(text || ''));
  return isNaN(t) ? null : t;
}

// ─── 요청 대기열 (이 파일만 쓴다) ────────────────────────────────────────────
function addRequest_(folder, item) {
  var doc = readJson_(folder, REQUESTS_FILE) || {items: []};
  doc.items = doc.items || [];
  var exists = doc.items.some(function (it) {
    return it.type === item.type && it.date === item.date && (it.label || '') === (item.label || '');
  });
  if (exists) return;
  item.id = Utilities.getUuid();
  item.at = nowIso_();
  doc.items.push(item);
  writeJson_(folder, REQUESTS_FILE, doc);
}

function needsRetitle_(game) {
  if (DONE_STATES.indexOf(game.status) < 0 || !game.tagged_at) return false;
  if (Number(game.retitle_attempts || 0) >= MAX_RETITLE_ATTEMPTS) return false;
  var tagged = parseTime_(game.tagged_at), done = parseTime_(game.retitled_at);
  return tagged !== null && (done === null || done < tagged);
}

/** 처리 여부는 대상 파일의 상태로 판단한다(러너는 대기열을 고치지 않는다). 모르는 종류(월간 — Phase 3)는 끝난 것으로 본다. */
function requestDone_(folder, item) {
  if (item.type === 'publish') {
    return !listGames_(folder, {date: item.date}).some(function (g) {
      return PUBLISH_STATES.indexOf(g.status) >= 0;
    });
  }
  if (item.type === 'retitle') {
    var game = readJson_(folder, gamesName_(item.date, item.label));
    return !game || !needsRetitle_(game);
  }
  return true;
}

/** 아직 안 끝난 대기열 항목 수 — tick_이 일거리로 센다(M10). `_state`가 없으면 0. */
function countPendingRequests_(rootId) {
  var folder = stateFolder_(rootId);
  if (!folder) return 0;
  var doc = readJson_(folder, REQUESTS_FILE);
  var items = (doc && doc.items) || [];
  return items.filter(function (it) { return !requestDone_(folder, it); }).length;
}

// ─── 화면용 데이터 ───────────────────────────────────────────────────────────
function recentOpponents_() {
  try {
    var raw = PropertiesService.getScriptProperties().getProperty(RECENT_OPPONENTS_KEY);
    var list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (e) {
    return [];
  }
}

function rememberOpponent_(name) {
  if (!name) return;
  var list = recentOpponents_().filter(function (n) { return n !== name; });
  list.unshift(name);
  PropertiesService.getScriptProperties().setProperty(
    RECENT_OPPONENTS_KEY, JSON.stringify(list.slice(0, RECENT_OPPONENTS_MAX)));
}

function cleanOpponent_(value) {
  return String(value == null ? '' : value).replace(/[\u0000-\u001f<>]/g, ' ').replace(/\s+/g, ' ')
    .trim().slice(0, OPPONENT_MAX);
}

function needsAttention_(game) {
  if (game.status === 'pending' || game.status === 'failed') return true;
  return game.status === 'auto_published' && !game.kind;          // 자동 게시된 뒤 아직 태깅 안 한 경기(늦은 태깅)
}

/** 클립 하나를 화면에 보낼 모양으로 — 공격/수비·월간 대상 여부는 서버가 계산해 화면은 그대로 그린다. */
function viewClip_(clip, game) {
  var cands = clip.cands || [];
  var shot = cands.filter(function (c) { return SHOT_TYPES.indexOf(String(c.type || '').toLowerCase()) >= 0; });
  var tag = clipTag_(game, clip.idx);
  var side = sideOf_(clip, game);
  return {
    idx: clip.idx, name: clip.name, fileId: clip.file_id, dur: clip.dur,
    srcStart: clip.src_start, end: clip.end || null,
    peak: cands.length ? cands[0].peak : null, shots: shot.length,
    side: side, sideOverride: tag.side_override || null,
    removed: !!tag.removed, conceded: !!tag.conceded && side === DEFENSE,
    monthly: monthlyEligible_([clip], game).length > 0,
    preview: 'https://drive.google.com/file/d/' + clip.file_id + '/preview',
    link: 'https://drive.google.com/file/d/' + clip.file_id + '/view'
  };
}

function viewGame_(game, clipsDoc) {
  var clips = (clipsDoc && clipsDoc.clips) || [];
  var weekly = weeklyClips_(clips, game);
  var eligible = monthlyEligible_(clips, game);
  var sum = function (list) { return list.reduce(function (s, c) { return s + Number(c.dur || 0); }, 0); };
  return {
    date: game.date, label: game.label, status: game.status,
    name: gameName_(game.date, game.label, game.kind, game.opponent),
    kind: game.kind || null, opponent: game.opponent || '', attackEnd: game.attack_end || null,
    switchAtIdx: game.switch_at_idx === undefined ? null : game.switch_at_idx,
    tagged: !!game.tagged_at, tagSource: game.tag_source || null, autoAt: game.auto_at || null,
    ytUrl: game.yt_url || null, error: game.error || null,
    editable: EDITABLE_STATES.indexOf(game.status) >= 0,
    publishable: PUBLISHABLE_STATES.indexOf(game.status) >= 0,
    hasEnd: clips.some(function (c) { return !!FLIP_END[c.end]; }),      // 골대 쪽 판별(L1) 결과가 있을 때만 골대 버튼을 보인다
    weeklyCount: weekly.length, weeklySec: Math.round(sum(weekly)),
    monthlyCount: eligible.length, monthlySec: Math.round(sum(eligible)),
    clipCount: clips.length,
    clips: clips.map(function (c) { return viewClip_(c, game); })
  };
}

// ─── 검토 페이지 서버 함수 (google.script.run으로 부른다 — 모두 소유자만) ──────
/** 검토가 필요한 경기와 최근 경기 목록. */
function reviewList() {
  assertOwner_();
  var folder = stateFolder_(rootFolderId_());
  var now = Date.now();
  var minDate = kstDate_(now - RECENT_DAYS * 86400000);
  var games = listGames_(folder, {minDate: minDate});
  var days = [];
  games.forEach(function (g) {
    var day = days.length && days[days.length - 1].date === g.date ? days[days.length - 1] : null;
    if (!day) { day = {date: g.date, games: []}; days.push(day); }
    var attention = needsAttention_(g);
    var clipsDoc = attention ? readJson_(folder, clipsName_(g.date, g.label)) : null;
    day.games.push({
      label: g.label, status: g.status, name: gameName_(g.date, g.label, g.kind, g.opponent),
      kind: g.kind || null, tagged: !!g.tagged_at, attention: attention,
      clips: clipsDoc ? (clipsDoc.clips || []).length : null, autoAt: g.auto_at || null, ytUrl: g.yt_url || null
    });
  });
  days.reverse();                                    // 최근 날짜가 위로
  return {today: kstDate_(now), days: days, opponents: recentOpponents_()};
}

/** 그날 경기들의 태깅 화면 데이터. */
function reviewGetDay(date) {
  assertOwner_();
  checkDate_(date);
  var folder = stateFolder_(rootFolderId_());
  var games = listGames_(folder, {date: date});
  return {
    date: date, opponents: recentOpponents_(),
    games: games.map(function (g) { return viewGame_(g, readJson_(folder, clipsName_(g.date, g.label))); })
  };
}

function toIdx_(value) {
  var n = Number(value);
  if (!isFinite(n) || n < 1 || Math.floor(n) !== n) throw new Error('클립 번호가 맞지 않습니다: ' + value);
  return n;
}

/**
 * 경기 단위 태그: kind(청백전/다른 팀) · opponent · attack_end(골대) · switch_at_idx("여기부터 반대").
 * 이미 올린 경기에서 이름이 바뀌면 제목 변경(retitle) 요청을 대기열에 넣고 러너를 깨운다.
 */
function reviewSetGame(date, label, fields) {
  assertOwner_();
  checkKey_(date, label);
  var result = withLock_(function () {
    var folder = requireState_();
    var game = loadGame_(folder, date, label);
    if (EDITABLE_STATES.indexOf(game.status) < 0) {
      throw new Error('지금은 고칠 수 없습니다(상태: ' + game.status + ') — 게시가 끝난 뒤에 고치세요');
    }
    var f = fields || {};
    if ('kind' in f) {
      if (f.kind && KINDS.indexOf(f.kind) < 0) throw new Error('종류가 맞지 않습니다: ' + f.kind);
      game.kind = f.kind || null;
    }
    if ('opponent' in f) game.opponent = cleanOpponent_(f.opponent);
    if ('attack_end' in f) {
      if (f.attack_end && !FLIP_END[f.attack_end]) throw new Error('골대 값이 맞지 않습니다: ' + f.attack_end);
      game.attack_end = f.attack_end || null;
    }
    if ('switch_at_idx' in f) {
      var s = f.switch_at_idx;
      game.switch_at_idx = (s === null || s === undefined || s === '') ? null : toIdx_(s);
    }
    game.tagged_at = nowIso_();
    game.tag_source = 'manual';
    saveGame_(folder, game);
    if (game.kind === KIND_OPPONENT && game.opponent) rememberOpponent_(game.opponent);

    var retitle = false;
    if (DONE_STATES.indexOf(game.status) >= 0 && game.yt_url &&
        gameName_(game.date, game.label, game.kind, game.opponent) !== (game.yt_title || '')) {
      addRequest_(folder, {type: 'retitle', date: date, label: label});
      retitle = true;
    }
    return {retitle: retitle, view: viewGame_(game, readJson_(folder, clipsName_(date, label)))};
  });
  if (result.retitle) tick({force: true});
  return result.view;
}

/**
 * 클립 하나의 태그: side_override('attack'|'defense'|null) · conceded(실점 — 수비 클립에서만) · removed(잘못 잡힘).
 * 주간 영상에서는 removed만 빠진다. conceded·side는 월간에만 쓰인다.
 */
function reviewTagClip(date, label, idx, tag) {
  assertOwner_();
  checkKey_(date, label);
  idx = toIdx_(idx);
  return withLock_(function () {
    var folder = requireState_();
    var game = loadGame_(folder, date, label);
    if (EDITABLE_STATES.indexOf(game.status) < 0) {
      throw new Error('지금은 고칠 수 없습니다(상태: ' + game.status + ') — 게시가 끝난 뒤에 고치세요');
    }
    var clipsDoc = readJson_(folder, clipsName_(date, label));
    var clip = ((clipsDoc && clipsDoc.clips) || []).filter(function (c) { return c.idx === idx; })[0];
    if (!clip) throw new Error('클립 ' + idx + '번이 없습니다');

    var patch = tag || {};
    var t = {};
    var current = clipTag_(game, idx);
    Object.keys(current).forEach(function (k) { t[k] = current[k]; });
    if ('side_override' in patch) {
      if (patch.side_override === ATTACK || patch.side_override === DEFENSE) t.side_override = patch.side_override;
      else delete t.side_override;
    }
    if ('removed' in patch) { if (patch.removed) t.removed = true; else delete t.removed; }
    if ('conceded' in patch) { if (patch.conceded) t.conceded = true; else delete t.conceded; }

    var tags = {};
    Object.keys(game.clip_tags || {}).forEach(function (k) { tags[k] = game.clip_tags[k]; });
    tags[String(idx)] = t;
    var probe = {kind: game.kind, attack_end: game.attack_end, switch_at_idx: game.switch_at_idx, clip_tags: tags};
    if (t.conceded && sideOf_(clip, probe) !== DEFENSE) {
      if (patch.conceded) throw new Error('실점은 수비 클립에서만 표시할 수 있습니다');
      delete t.conceded;                              // 수비가 아니게 바뀌었으면 실점 표시도 지운다
    }
    if (Object.keys(t).length) tags[String(idx)] = t;
    else delete tags[String(idx)];
    game.clip_tags = tags;
    game.tagged_at = nowIso_();
    game.tag_source = 'manual';
    saveGame_(folder, game);
    return viewGame_(game, clipsDoc);
  });
}

/**
 * "게시": 검토를 마친 경기를 publish로 바꾸고 대기열에 넣은 뒤 러너를 깨운다.
 * 종류(청백전/다른 팀+상대 이름)를 안 골랐으면 거부한다. 실패(failed)한 경기는 "다시 게시".
 * labels가 비어 있으면 그날 게시할 수 있는 경기 전부.
 */
function reviewPublish(date, labels) {
  assertOwner_();
  checkDate_(date);
  var out = withLock_(function () {
    var folder = requireState_();
    var games = listGames_(folder, {date: date});
    var wanted = (labels && labels.length) ? labels : games.filter(function (g) {
      return PUBLISHABLE_STATES.indexOf(g.status) >= 0;
    }).map(function (g) { return g.label; });
    var started = [], skipped = [];
    wanted.forEach(function (label) {
      var game = games.filter(function (g) { return g.label === label; })[0];
      var reason = null;
      if (!game) reason = '경기 기록이 없습니다';
      else if (PUBLISHABLE_STATES.indexOf(game.status) < 0) reason = '지금은 게시할 수 없는 상태입니다(' + game.status + ')';
      else if (KINDS.indexOf(game.kind) < 0) reason = '종류(청백전 / 다른 팀)를 먼저 고르세요';
      else if (game.kind === KIND_OPPONENT && !cleanOpponent_(game.opponent)) reason = '상대 팀 이름을 적어 주세요';
      if (reason) { skipped.push({label: label, reason: reason}); return; }
      game.status = 'publish';
      game.attempts = 0;                              // 사용자가 다시 누른 것 — 죽은 실행의 시도 횟수와 따로 센다
      game.error = null;
      game.tag_source = 'manual';
      if (!game.tagged_at) game.tagged_at = nowIso_();
      saveGame_(folder, game);
      started.push(label);
    });
    if (started.length) addRequest_(folder, {type: 'publish', date: date});
    return {started: started, skipped: skipped};
  });
  if (out.started.length) out.tick = tick({force: true});
  return out;
}

/** 최근에 쓴 상대 이름(칩). */
function reviewRecentOpponents() {
  assertOwner_();
  return recentOpponents_();
}

// ─── 매일 09:00 ─────────────────────────────────────────────────────────────
/**
 * 시간 트리거(installDailyTrigger)가 부른다.
 *  1) 자동 게시 시각(auto_at)이 지난 pending 경기를 publish로 바꾼다 — 검토 없이 전체 장면으로 올라간다.
 *     태그를 일부 눌러 둔 경기는 그 태그(잘못 잡힘·상대 이름)를 그대로 쓴다(tag_source=manual).
 *  2) 끝난 대기열 항목을 정리한다.
 *  3) 자동 게시한 경기가 있으면 러너를 깨우고 알림을 보낸다(누르면 검토 페이지 — 늦은 태깅).
 */
function dailyJobs() {
  try {
    var result = withLock_(dailyJobs_);
    if (result.names.length) result.tick = tick({force: true});
    return log_('자동 게시 ' + result.names.length + '경기' +
      (result.names.length ? ' (' + result.names.join(', ') + ')' : '') +
      ' · 정리한 요청 ' + result.cleaned + '건');
  } catch (e) {
    console.error(e);
    return '오류: ' + (e && e.message ? e.message : e);
  }
}

function dailyJobs_() {
  var folder = stateFolder_(rootFolderId_());
  var result = {names: [], cleaned: 0};
  if (!folder) return result;
  var now = Date.now();
  var minDate = kstDate_(now - AUTO_LOOKBACK_DAYS * 86400000);
  var dates = [];
  listGames_(folder, {minDate: minDate}).forEach(function (g) {
    var at = parseTime_(g.auto_at);
    if (g.status !== 'pending' || at === null || at > now) return;
    g.status = 'publish';
    g.tag_source = g.tagged_at ? 'manual' : 'auto';
    g.attempts = 0;
    g.error = null;
    saveGame_(folder, g);
    result.names.push(gameName_(g.date, g.label, g.kind, g.opponent));
    if (dates.indexOf(g.date) < 0) dates.push(g.date);
  });
  dates.forEach(function (d) { addRequest_(folder, {type: 'publish', date: d}); });

  var doc = readJson_(folder, REQUESTS_FILE);
  if (doc && doc.items) {
    var keep = doc.items.filter(function (it) { return !requestDone_(folder, it); });
    result.cleaned = doc.items.length - keep.length;
    if (result.cleaned) { doc.items = keep; writeJson_(folder, REQUESTS_FILE, doc); }
  }

  if (result.names.length) {
    try {
      var props = PropertiesService.getScriptProperties();
      var base = props.getProperty('REVIEW_URL');
      var click = base ? base + (base.indexOf('?') >= 0 ? '&' : '?') + 'page=review' +
        (dates.length === 1 ? '&date=' + dates[0] : '') : null;
      notify_(readConfig_(props), result.names.length + '경기를 검토 없이 자동 게시합니다',
              result.names.join('\n') + '\n\n게시가 끝나면 BAND 글 알림이 옵니다. ' +
              '상대팀·청백전은 나중에 태깅해도 제목에 반영됩니다(이 알림을 누르면 검토 페이지).',
              ['soccer'], click);
    } catch (e) {
      console.warn('자동 게시 알림 실패: ' + e);
    }
  }
  return result;
}
