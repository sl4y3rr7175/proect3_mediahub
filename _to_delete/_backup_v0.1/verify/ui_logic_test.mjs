#!/usr/bin/env node
/* ==========================================================================
   Проверка клиентской логики web/app.js без браузера.

   Скрипт поднимает минимальный DOM-шим (ровно те методы, которые использует
   клиент), загружает app.js, эмулирует события выбора файлов и проверяет
   инспектор типов, построение настроек, сборку параметров запроса и переходы
   состояний интерфейса.

   Запуск: node verify/ui_logic_test.mjs
   ========================================================================== */
'use strict';

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(HERE, '..');
const APP_JS = path.join(PROJECT_ROOT, 'web', 'app.js');

/* --- Минимальный DOM ------------------------------------------------------ */
class ClassList {
  constructor() { this.set = new Set(); }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
  get value() { return [...this.set].join(' '); }
}

/** Минимальный аналог CSSStyleDeclaration: свойства и custom-properties. */
class Style {
  constructor() { this.properties = new Map(); }
  setProperty(name, value) { this.properties.set(name, String(value)); }
  removeProperty(name) { this.properties.delete(name); }
  getPropertyValue(name) { return this.properties.get(name) || ''; }
  get cssText() {
    return [...this.properties].map(([k, v]) => `${k}:${v}`).join(';');
  }
}

class Element {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentNode = null;
    this.classList = new ClassList();
    this.dataset = {};
    this.style = new Style();
    this.attributes = {};
    this.listeners = new Map();
    this.textContent = '';
    this.value = '';
    this.type = '';
    this.disabled = false;
    this.checked = false;
    this.files = null;
    this._innerHTML = '';
  }

  get className() { return this.classList.value; }
  set className(value) {
    this.classList = new ClassList();
    String(value).split(/\s+/).filter(Boolean).forEach((n) => this.classList.add(n));
  }

  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) { this._innerHTML = String(value); this.children = []; }

  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatchEvent(event) {
    const handlers = this.listeners.get(event.type) || [];
    event.target = event.target || this;
    handlers.forEach((handler) => handler.call(this, event));
    return true;
  }

  click() { this.dispatchEvent({ type: 'click' }); }
  scrollIntoView() {}
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(selector) { return this._query(selector, true); }
  querySelectorAll(selector) { return this._query(selector, false); }

  /** Разбор простых селекторов: тег, .класс, [attr=value], #id, вложенность через пробел. */
  _matches(selector) {
    const parts = selector.trim().split(/\s+/);
    let node = this;
    for (let i = parts.length - 1; i >= 0; i -= 1) {
      if (!node || !matchesSimple(node, parts[i])) return false;
      node = i > 0 ? node.parentNode : null;
    }
    return true;
  }

  _query(selector, first) {
    const results = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child._matches(selector)) {
          results.push(child);
          if (first) return true;
        }
        if (walk(child) && first) return true;
      }
      return false;
    };
    walk(this);
    return first ? (results[0] || null) : results;
  }
}

function matchesSimple(node, part) {
  const attrMatch = part.match(/^\[([\w-]+)(?:=("?)([^"\]]*)\2)?\]$/);
  if (attrMatch) {
    const [, name, , value] = attrMatch;
    const actual = name === 'type' ? node.type : node[name] !== undefined ? node[name] : node.attributes[name];
    if (value === undefined) return actual !== undefined;
    // В DOM значения атрибутов строковые; приводим булевы свойства так же.
    return String(actual) === value;
  }
  if (part.startsWith('.')) return node.classList.contains(part.slice(1));
  if (part.startsWith('#')) return node.attributes.id === part.slice(1);
  return node.tagName === part.toUpperCase();
}

/** Клик, как его видит браузер: сначала свойство onclick, затем слушатели. */
function clickNode(node) {
  if (typeof node.onclick === 'function') node.onclick({ type: 'click', target: node });
  node.dispatchEvent({ type: 'click', target: node });
}

/** Изменение значения поля: свойство oninput, затем слушатели. */
function inputNode(node, value) {
  node.value = value;
  if (typeof node.oninput === 'function') node.oninput({ type: 'input', target: node });
  node.dispatchEvent({ type: 'input', target: node });
}

class Document extends Element {
  constructor() {
    super('#document', null);
    this.ownerDocument = this;
    this.readyState = 'complete';
    this.listeners = new Map();
  }
  createElement(tag) { return new Element(tag, this); }
  getElementById(id) {
    const found = this._query(`#${id}`, true);
    return found;
  }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatchEvent(event) {
    (this.listeners.get(event.type) || []).forEach((handler) => handler.call(this, event));
    return true;
  }
}

/* --- Разметка, повторяющая web/index.html -------------------------------- */
const IDS = [
  'notice', 'config-card', 'file-list', 'file-count', 'files-summary', 'options',
  'convert-button', 'clear-button', 'progress-wrap', 'progress-bar', 'progress-text',
  'result-card', 'metrics', 'result-meta', 'compare-before', 'compare-after', 'compare-before-size', 'compare-after-size', 'downloads', 'warnings', 'download-button', 'qr-button',
  'reset-button', 'qr-modal', 'qr-image', 'qr-url', 'qr-close', 'status-badge',
  'net-info', 'dropzone-title', 'steps', 'hero',
];

function buildDocument() {
  const document = new Document();
  const body = document.createElement('body');
  document.appendChild(body);

  IDS.forEach((id) => {
    const node = document.createElement('div');
    node.setAttribute('id', id);
    body.appendChild(node);
  });

  const dropzone = document.createElement('div');
  dropzone.setAttribute('id', 'dropzone');
  body.appendChild(dropzone);

  const input = document.createElement('input');
  input.setAttribute('id', 'file-input');
  input.type = 'file';
  body.appendChild(input);

  document.body = body;
  return document;
}

/* --- Загрузка app.js в поддельное окружение ------------------------------ */
function loadApp(document) {
  const code = fs.readFileSync(APP_JS, 'utf8');
  const sandbox = {
    document,
    window: {
      addEventListener() {},
      scrollTo() {},
      innerWidth: 1280,
      location: { search: '' },  // демо-режим в тестах не активируется
      matchMedia: () => ({ matches: false, addEventListener() {} }),
    },
    fetch: async () => ({ ok: true, json: async () => ({ network: {}, ffmpeg: { available: true } }) }),
    FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } },
    URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  const runner = new Function(
    'document', 'window', 'fetch', 'FormData', 'URL', 'setInterval', 'clearInterval', 'console',
    `${code}\n;return { state, inspectFiles, render, renderOptions, buildOptions, fileKind, formatBytes, extensionOf, DEFAULT_OPTIONS };`,
  );
  return runner(
    sandbox.document, sandbox.window, sandbox.fetch, sandbox.FormData, sandbox.URL,
    sandbox.setInterval, sandbox.clearInterval, sandbox.console,
  );
}

function makeFile(name, size = 2048, type = '') {
  return { name, size, type };
}

// Диагностика: при UI_TRACE=1 обёртки печатают состояние после каждого шага.
function traced(app, names) {
  return Object.fromEntries(
    Object.entries(app).map(([key, value]) => [
      key,
      typeof value === 'function' && names.includes(key)
        ? (...args) => {
            const result = value(...args);
            trace(`${key}(${args.length ? String(args[0]).slice(0, 40) : ''}) -> состояние: `
              + `файлов=${app.state.files.length}, вид=${app.state.kind}`);
            return result;
          }
        : value,
    ]),
  );
}

/* --- Проверки ------------------------------------------------------------- */
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? ` – ${detail}` : ''}`);
}

const document = buildDocument();
const rawApp = loadApp(document);
// В браузере это событие отправляет движок после разбора разметки: именно на нём
// клиент навешивает обработчики (web/app.js, document.addEventListener).
document.dispatchEvent({ type: 'DOMContentLoaded' });

const input = document.getElementById('file-input');

if (process.env.UI_TRACE) {
  trace(`слушателей change на #file-input: ${(input.listeners.get('change') || []).length}`);
}

const app = rawApp;

function selectFiles(files) {
  input.files = files;
  try {
    input.dispatchEvent({ type: 'change' });
  } catch (error) {
    trace(`исключение в обработчике: ${error && error.stack ? error.stack.split('\n')[0] : error}`);
    throw error;
  }
}

function trace(message) {
  if (process.env.UI_TRACE) console.log(`      · ${message}`);
}

console.log('MediaHub – проверка клиентской логики (Node + DOM-шим)\n');

console.log('[1] Инспектор типов');
check('определение изображения по расширению', app.fileKind(makeFile('фото.HEIC')) === 'image');
check('определение изображения по MIME', app.fileKind(makeFile('scan', 10, 'image/png')) === 'image');
check('определение PDF', app.fileKind(makeFile('док.pdf')) === 'pdf');
check('определение видео', app.fileKind(makeFile('clip.MKV')) === 'video');
check('определение аудио', app.fileKind(makeFile('song.flac')) === 'video');
check('неизвестный формат отклоняется', app.fileKind(makeFile('virus.exe')) === null);

selectFiles([makeFile('photo.png', 1024, 'image/png')]);
check('одно изображение принято', app.state.files.length === 1 && app.state.kind === 'image');
check('сессия начинается пустой (после выбора 1 файл)', app.state.files.length === 1);

console.log('\n[2] Блокировка смешения типов');
app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('photo.png', 1024, 'image/png'), makeFile('doc.pdf', 1024, 'application/pdf')]);
check('изображение + PDF отклонены', app.state.files.length === 0 && app.state.kind === null);
const noticeText = document.getElementById('notice').textContent;
check(
  'показано предупреждение «файлы одного типа»',
  document.getElementById('notice').classList.contains('show') && noticeText.includes('одного типа'),
  noticeText,
);

console.log('\n[3] Лимиты');
app.state.files = [];
app.state.kind = null;
const many = Array.from({ length: 51 }, (_, i) => makeFile(`img${i}.png`, 10, 'image/png'));
selectFiles(many);
check('больше 50 файлов отклонено', app.state.files.length === 0,
  document.getElementById('notice').textContent);

app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('big.png', 201 * 1024 * 1024, 'image/png')]);
check('файл тяжелее 200 МБ отклонён', app.state.files.length === 0,
  document.getElementById('notice').textContent);

console.log('\n[4] Настройки изображений');
app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('a.png', 1024, 'image/png'), makeFile('b.png', 1024, 'image/png')]);
check('панель настроек раскрылась',
  !document.getElementById('config-card').classList.contains('hidden'));
check('в списке две строки', document.getElementById('file-list').children.length === 2);
const chips = document.getElementById('options').querySelectorAll('.chip');
check('показаны чипы JPG/PNG/WEBP',
  chips.map((c) => c.textContent).join(',') === 'JPG,PNG,WEBP',
  chips.map((c) => c.textContent).join(','));

const imageChips = document.getElementById('options').querySelectorAll('.chip');
clickNode(imageChips[2]);  // WEBP
check('переключение формата на WEBP', app.state.options.image.format === 'webp');

const options = app.buildOptions();
check('параметры запроса: формат и качество',
  options.format === 'webp' && options.quality === 85, JSON.stringify(options));

// Ползунок качества: значение и числовой индикатор.
const slider = document.getElementById('options').querySelectorAll('input')
  .find((node) => node.type === 'range');
inputNode(slider, '40');
const qualityLabel = document.getElementById('options').querySelector('.range-value');
check('ползунок качества меняет параметр', app.buildOptions().quality === 40,
  `quality=${app.buildOptions().quality}`);
check('числовой индикатор обновляется', qualityLabel.textContent === '40%', qualityLabel.textContent);

console.log('\n[5] Настройки PDF');
app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('one.pdf', 1024, 'application/pdf')]);
let pdfChips = document.getElementById('options').querySelectorAll('.chip').map((c) => c.textContent);
check('для одного PDF – сжать или в картинки',
  pdfChips.some((t) => t.includes('Сжать')) && pdfChips.some((t) => t.includes('картинки')),
  pdfChips.join(' | '));
clickNode(document.getElementById('options').querySelectorAll('.chip')[1]);  // в картинки
const pdfOptions = app.buildOptions();
check('параметры рендера содержат DPI', pdfOptions.action === 'to_images' && pdfOptions.dpi === 150,
  JSON.stringify(pdfOptions));

app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('a.pdf', 1024, 'application/pdf'), makeFile('b.pdf', 1024, 'application/pdf')]);
const mergeOptions = app.buildOptions();
check('для нескольких PDF выбран режим склейки', mergeOptions.action === 'merge',
  JSON.stringify(mergeOptions));
const draggable = document.getElementById('file-list').querySelectorAll('[draggable="true"]');
check('строки PDF перетаскиваемые (порядок склейки)', draggable.length === 2,
  `перетаскиваемых строк: ${draggable.length}`);

console.log('\n[6] Настройки видео');
app.state.files = [];
app.state.kind = null;
selectFiles([makeFile('clip.mp4', 4096, 'video/mp4')]);
let videoChips = document.getElementById('options').querySelectorAll('.chip').map((c) => c.textContent);
check('три действия для видео', videoChips.length === 3, videoChips.join(' | '));

clickNode(document.getElementById('options').querySelectorAll('.chip')[0]);  // MP3
let videoOptions = app.buildOptions();
check('MP3: битрейт по умолчанию 192', videoOptions.action === 'mp3' && videoOptions.bitrate === 192,
  JSON.stringify(videoOptions));

const gifChip = document.getElementById('options').querySelectorAll('.chip')
  .find((c) => c.textContent.includes('GIF'));
clickNode(gifChip);
videoOptions = app.buildOptions();
check('GIF: fps, ширина, длительность',
  videoOptions.action === 'gif' && videoOptions.fps === 12 && videoOptions.width === 480 &&
  videoOptions.seconds === 15, JSON.stringify(videoOptions));
const numberInputs = document.getElementById('options').querySelectorAll('input');
check('появились числовые поля GIF', numberInputs.length === 3, `полей: ${numberInputs.length}`);

const compressChip = document.getElementById('options').querySelectorAll('.chip')
  .find((c) => c.textContent.includes('Сжать'));
clickNode(compressChip);
videoOptions = app.buildOptions();
check('сжатие видео: CRF 27', videoOptions.action === 'compress' && videoOptions.crf === 27,
  JSON.stringify(videoOptions));

console.log('\n[7] Утилиты форматирования');
check('байты', app.formatBytes(512) === '512 Б', app.formatBytes(512));
check('килобайты', app.formatBytes(2048) === '2,0 КБ', app.formatBytes(2048));
check('мегабайты', app.formatBytes(14.2 * 1048576) === '14,2 МБ', app.formatBytes(14.2 * 1048576));
check('гигабайты', app.formatBytes(3 * 1073741824) === '3,00 ГБ', app.formatBytes(3 * 1073741824));
check('расширение в нижнем регистре', app.extensionOf('Photo.HEIC') === 'heic');

console.log('\n[8] Состояние после сброса');
app.state.files = [];
app.state.kind = null;
app.render();
check('панель настроек скрыта без файлов',
  document.getElementById('config-card').classList.contains('hidden'));

const failed = results.filter((r) => !r.ok);
console.log('\n' + '='.repeat(68));
console.log(`Проверок: ${results.length}, провалено: ${failed.length}`);
if (failed.length) {
  failed.forEach((r) => console.log(`  ✗ ${r.name} – ${r.detail}`));
  process.exit(1);
}
console.log('Логика клиента соответствует ТЗ (раздел 5).');
