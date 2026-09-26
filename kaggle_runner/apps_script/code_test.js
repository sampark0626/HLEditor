// Code.gs를 가짜 Apps Script 환경에서 시험한다 — 실행: node kaggle_runner/apps_script/code_test.js
// Apps Script 전역(PropertiesService, DriveApp, UrlFetchApp …)을 흉내 내고 시나리오별로 확인한다.
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SOURCE = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');
const KAGGLE = 'https://api.kaggle.com/v1/kernels.KernelsApiService/';

// 2026-09-27(일) 12:00 KST = 03:00 UTC
const SUNDAY_NOON = Date.UTC(2026, 8, 27, 3, 0, 0);
const WEDNESDAY_NOON = Date.UTC(2026, 8, 30, 3, 0, 0);
const MIN = 60 * 1000;

function makeEnv({files = {}, status = 'COMPLETE', saveResponse = null, bootstrapOk = true,
                  props = {}, now = SUNDAY_NOON} = {}) {
  const env = {now, calls: [], status, saveResponse, bootstrapOk, triggers: []};
  const store = Object.assign({
    KAGGLE_USERNAME: 'me', KAGGLE_API_TOKEN: 'KGAT_x', ROOT_FOLDER_ID: 'root', NTFY_TOPIC: 'hl-topic',
  }, props);

  class FakeDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(env.now); }
    static now() { return env.now; }
  }

  const iterator = (items) => { let i = 0; return {hasNext: () => i < items.length, next: () => items[i++]}; };
  const folder = (name) => ({
    getFiles: () => iterator((env.files[name] || []).map((f) => ({
      getName: () => f.name, getMimeType: () => f.mime || 'application/octet-stream', isTrashed: () => !!f.trashed,
    }))),
  });
  env.files = files;

  const response = (code, body) => ({
    getResponseCode: () => code,
    getContentText: () => (typeof body === 'string' ? body : JSON.stringify(body)),
  });

  const context = {
    Date: FakeDate,
    JSON, Number, String, Math, Error, Object, Array, RegExp,
    console: {error: () => {}, warn: () => {}, log: () => {}},
    PropertiesService: {getScriptProperties: () => ({
      getProperty: (k) => (k in store ? store[k] : null),
      setProperty: (k, v) => { store[k] = String(v); },
      deleteProperty: (k) => { delete store[k]; },
      getProperties: () => Object.assign({}, store),
    })},
    LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock: () => {}})},
    DriveApp: {getFolderById: (id) => {
      assert.strictEqual(id, 'root');
      return {getFoldersByName: (name) => iterator(name in env.files ? [folder(name)] : [])};
    }},
    Utilities: {base64Encode: (s) => Buffer.from(s, 'utf8').toString('base64'), formatDate: (date, tz, fmt) => {
      assert.strictEqual(tz, 'Asia/Seoul');
      assert.strictEqual(fmt, 'u');
      const day = new Date(date.getTime() + 9 * 3600 * 1000).getUTCDay();  // 0=일
      return String(day === 0 ? 7 : day);
    }},
    UrlFetchApp: {fetch: (url, opts = {}) => {
      env.calls.push({url, opts, body: opts.payload ? JSON.parse(opts.payload) : null});
      if (url.startsWith(KAGGLE + 'GetKernelSessionStatus')) {
        if (env.status === 404) return response(404, {error: 'not found'});
        if (env.status === 403) {   // 실제 Kaggle: 없는 커널 조회는 403 kernels.get
          return response(403, {error: {code: 403, message: "Permission 'kernels.get' was denied",
                                        status: 'PERMISSION_DENIED'}});
        }
        if (env.status === 401) return response(401, {error: {code: 401, message: 'Unauthenticated'}});
        return response(200, {status: env.status});
      }
      if (url.startsWith(KAGGLE + 'SaveKernel')) {
        return response(200, env.saveResponse || {ref: '/code/me/hleditor-inbox', versionNumber: 3});
      }
      if (url.includes('raw.githubusercontent.com')) {
        if (!env.bootstrapOk) throw new Error('network down');
        return response(200, 'print("from github")  # inbox_runner');
      }
      if (url === 'https://ntfy.sh/') return response(200, {id: 'n1'});
      throw new Error('unexpected url ' + url);
    }},
    ContentService: {createTextOutput: (text) => ({text})},
    ScriptApp: {
      getProjectTriggers: () => env.triggers.map((t) => ({getHandlerFunction: () => t.fn})),
      deleteTrigger: () => { env.triggers.pop(); },
      newTrigger: (fn) => ({timeBased: () => ({everyMinutes: (n) => ({create: () => env.triggers.push({fn, n})})})}),
    },
  };
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  env.ctx = context;
  env.props = store;
  env.of = (prefix) => env.calls.filter((c) => c.url.startsWith(prefix));
  return env;
}

const video = (name) => ({name, mime: 'video/mp4'});
const tests = [];
const test = (name, fn) => tests.push({name, fn});

test('인박스가 비면 아무 API도 부르지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [], '01_inbox_2d': []}});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.calls.length, 0);
});

test('영상이 있고 커널이 쉬고 있으면 CPU로 push하고 시작 알림을 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.MP4'), video('b.mov'), {name: 'note.txt'}]}});
  const msg = env.ctx.tick();
  assert.match(msg, /영상 2개 발견/);
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.ok(push, 'SaveKernel 호출');
  assert.strictEqual(push.opts.headers.Authorization, 'Bearer KGAT_x');
  assert.strictEqual(push.body.slug, 'me/hleditor-inbox');
  assert.strictEqual(push.body.newTitle, 'hleditor-inbox');
  assert.strictEqual(push.body.kernelType, 'script');
  assert.strictEqual(push.body.isPrivate, true);
  assert.strictEqual(push.body.enableInternet, true);
  assert.strictEqual(push.body.enableGpu, false);
  assert.strictEqual(push.body.machineShape, undefined);
  assert.deepStrictEqual(push.body.datasetDataSources, ['me/hl-secrets']);
  assert.match(push.body.text, /from github/);
  const note = env.of('https://ntfy.sh/')[0];
  assert.strictEqual(note.body.topic, 'hl-topic');
  assert.match(note.body.title, /영상 2개 확인/);
  assert.strictEqual(env.props.LAST_PUSH_AT, String(SUNDAY_NOON));
});

test('01_inbox_2d에 영상이 있으면 GPU로 push한다', () => {
  const env = makeEnv({files: {'01_inbox': [], '01_inbox_2d': [video('c.mp4')]}});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.strictEqual(push.body.machineShape, 'NvidiaTeslaT4');
  assert.strictEqual(push.body.enableGpu, true);
});

test('커널이 실행 중이면 push하지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 'KERNEL_WORKER_STATUS_RUNNING'});
  assert.match(env.ctx.tick(), /처리 중/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 0);
});

test('커널이 아직 없으면(404) 첫 push로 만든다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 404});
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
});

test('커널이 아직 없을 때 Kaggle이 주는 403(kernels.get)도 "없음"으로 보고 첫 push로 만든다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 403});
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
  assert.match(env.ctx.showStatus(), /IDLE/);
});

test('다른 인증 오류(401)는 push하지 않고 오류로 알린다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, status: 401});
  assert.match(env.ctx.tick(), /^오류: Kaggle 상태 조회 실패 \(HTTP 401\)/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 0);
});

test('push 직후 10분 동안은 다시 push하지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  env.ctx.tick();
  env.now += 5 * MIN;
  env.status = 'COMPLETE';   // 상태가 아직 안 바뀐 것처럼 보이는 경우
  assert.match(env.ctx.tick(), /방금 실행을 요청/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 1);
  env.now += 6 * MIN;
  assert.match(env.ctx.tick(), /실행 요청함/);
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, 2);
});

test('확인 요일이 아니면 약 1시간에 한 번만 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': []}, now: WEDNESDAY_NOON});
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');           // 첫 확인은 진행
  env.now += 5 * MIN;
  assert.match(env.ctx.tick(), /건너뜀/);
  env.now += 51 * MIN;                                            // 첫 확인 후 56분
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
});

test('FAST_DAYS 기본값은 일·월 — 월요일엔 매번 확인한다', () => {
  const monday = Date.UTC(2026, 8, 28, 3, 0, 0);
  const env = makeEnv({files: {'01_inbox': []}, now: monday});
  env.ctx.tick();
  env.now += 5 * MIN;
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.props.LAST_SLOW_CHECK_AT, undefined);
});

test('FAST_DAYS를 바꾸면 그 요일에 촘촘히 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': []}, now: WEDNESDAY_NOON, props: {FAST_DAYS: '3'}});
  env.ctx.tick();
  env.now += 5 * MIN;
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
});

test('연속 실패 3회면 자동 실행을 멈추고 알림은 한 번만 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  for (let i = 0; i < 3; i++) {
    env.ctx.tick();                    // push
    env.now += 11 * MIN;
    env.status = 'ERROR';              // 다음 확인에서 이 실행의 실패를 센다
  }
  const pushes = env.of(KAGGLE + 'SaveKernel').length;
  assert.strictEqual(pushes, 3);
  assert.match(env.ctx.tick(), /자동 실행이 멈춰/);   // 세 번째 실패는 다음 확인에서 세고 바로 멈춘다
  assert.strictEqual(env.props.ERROR_STREAK, '3');
  env.now += 11 * MIN;
  env.ctx.tick();
  assert.strictEqual(env.of(KAGGLE + 'SaveKernel').length, pushes);
  const breakerNotes = env.of('https://ntfy.sh/').filter((c) => /자동 실행 중지/.test(c.body.title));
  assert.strictEqual(breakerNotes.length, 1);
  env.ctx.resetBreaker();
  env.status = 'COMPLETE';
  assert.match(env.ctx.tick(), /실행 요청함/);
});

test('성공한 실행을 확인하면 실패 횟수를 0으로 되돌린다(인박스가 비어 있어도)', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, props: {ERROR_STREAK: '2'}});
  env.ctx.tick();                      // push
  env.files['01_inbox'] = [];
  env.now += 30 * MIN;
  env.status = 'COMPLETE';
  assert.strictEqual(env.ctx.tick(), '새 영상 없음');
  assert.strictEqual(env.props.ERROR_STREAK, '0');
  const statusCalls = env.of(KAGGLE + 'GetKernelSessionStatus').length;
  env.now += 5 * MIN;
  env.ctx.tick();                      // 결과를 이미 기록했으므로 상태 조회 없음
  assert.strictEqual(env.of(KAGGLE + 'GetKernelSessionStatus').length, statusCalls);
});

test('SaveKernel이 거절하면 오류를 돌려주고 LAST_PUSH_AT을 남기지 않는다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]},
    saveResponse: {error: 'Notebook not found', invalidDatasetSources: ['me/hl-secrets']}});
  const msg = env.ctx.tick();
  assert.match(msg, /^오류: Kaggle 실행 요청 실패/);
  assert.match(msg, /invalidDatasetSources/);
  assert.strictEqual(env.props.LAST_PUSH_AT, undefined);
  assert.strictEqual(env.of('https://ntfy.sh/').length, 0);
});

test('GitHub에서 bootstrap을 못 받으면 내장 사본으로 push한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, bootstrapOk: false});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.match(push.body.text, /inbox_runner\.main\(\[\]\)/);
});

test('내장 bootstrap 사본은 저장소 kernel_bootstrap.py의 실행 코드와 같다', () => {
  const env = makeEnv();
  const file = fs.readFileSync(path.join(__dirname, '..', 'kernel_bootstrap.py'), 'utf8');
  const code = (text) => text.replace(/"""[\s\S]*?"""/, '').split('\n')
    .map((l) => l.replace(/\s+#.*$/, '').trim()).filter(Boolean).join('\n');
  assert.strictEqual(code(env.ctx.BOOTSTRAP_FALLBACK), code(file));
});

test('실행 링크: key가 틀리면 거부, 맞으면 요일과 상관없이 확인한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, now: WEDNESDAY_NOON,
    props: {KICK_KEY: 'secret', LAST_SLOW_CHECK_AT: String(WEDNESDAY_NOON)}});
  assert.match(env.ctx.doGet({parameter: {key: 'nope'}}).text, /거부됨/);
  assert.match(env.ctx.doGet({parameter: {}}).text, /거부됨/);
  assert.strictEqual(env.of(KAGGLE).length, 0);
  assert.match(env.ctx.doGet({parameter: {key: 'secret'}}).text, /실행 요청함/);
});

test('실행 링크: KICK_KEY를 설정하지 않았으면 항상 거부한다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}});
  assert.match(env.ctx.doGet({parameter: {key: ''}}).text, /거부됨/);
});

test('예전 방식 API 키(kaggle.json의 key)는 아이디와 함께 Basic 인증으로 보낸다', () => {
  const env = makeEnv({files: {'01_inbox': [video('a.mp4')]}, props: {KAGGLE_API_TOKEN: 'abc123'}});
  env.ctx.tick();
  const push = env.of(KAGGLE + 'SaveKernel')[0];
  assert.strictEqual(push.opts.headers.Authorization, 'Basic ' + Buffer.from('me:abc123').toString('base64'));
});

test('스크립트 속성이 빠지면 무엇이 빠졌는지 알려준다', () => {
  const env = makeEnv({props: {NTFY_TOPIC: ''}});
  assert.match(env.ctx.tick(), /스크립트 속성 누락: NTFY_TOPIC/);
});

test('installTrigger는 기존 tick 트리거를 지우고 5분 트리거 하나만 건다', () => {
  const env = makeEnv();
  env.ctx.installTrigger();
  env.ctx.installTrigger();
  assert.deepStrictEqual(env.triggers, [{fn: 'tick', n: 5}]);
});

let failed = 0;
for (const t of tests) {
  try {
    t.fn();
    console.log('ok   - ' + t.name);
  } catch (e) {
    failed++;
    console.log('FAIL - ' + t.name + '\n       ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n       ') : e));
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
