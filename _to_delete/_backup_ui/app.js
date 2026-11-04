/* ==========================================================================
   MediaHub – клиентская логика одностраничного интерфейса.

   Без внешних зависимостей: drag-and-drop, инспектор типов, настройки,
   опрос статуса задачи, карточка результата и QR-мост.
   ========================================================================== */
'use strict';

// --- Справочники типов ------------------------------------------------------
const IMAGE_EXT = ['jpg', 'jpeg', 'jpe', 'jfif', 'png', 'webp', 'heic', 'heif', 'bmp', 'tif', 'tiff', 'avif'];
const VIDEO_EXT = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v', 'wmv', 'flv', 'mpeg', 'mpg', 'ts', '3gp'];
const AUDIO_EXT = ['mp3', 'm4a', 'aac', 'wav', 'flac', 'ogg', 'opus'];

const DEFAULT_OPTIONS = {
  image: { format: 'jpg', quality: 85, targetKb: 0, maxWidth: 0, stripMetadata: true },
  pdf: { action: 'compress', dpi: 150 },
  video: { action: 'mp3', bitrate: 192, gifFps: 12, gifWidth: 480, gifSeconds: 15, crf: 27 },
};

//: Линейные иконки для файлов, у которых нет превью.
const FILE_ICONS = {
  pdf: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  video: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4.5-2.5z"/></svg>',
};

// --- Состояние --------------------------------------------------------------
const state = {
  files: [],
  kind: null,
  options: JSON.parse(JSON.stringify(DEFAULT_OPTIONS)),
  job: null,
  polling: null,
  network: { base_url: '' },
  ffmpeg: { available: true },
  dragIndex: null,
};

const el = (id) => document.getElementById(id);

// --- Утилиты ----------------------------------------------------------------
function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return '–';
  // Русская типографика: разделитель дробной части – запятая.
  const fixed = (value, digits) => value.toFixed(digits).replace('.', ',');
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${fixed(bytes / 1024, 1)} КБ`;
  if (bytes < 1024 * 1024 * 1024) return `${fixed(bytes / 1048576, 1)} МБ`;
  return `${fixed(bytes / 1073741824, 2)} ГБ`;
}

function extensionOf(name) {
  const parts = String(name).toLowerCase().split('.');
  return parts.length > 1 ? parts.pop() : '';
}

function fileKind(file) {
  const ext = extensionOf(file.name);
  const mime = (file.type || '').toLowerCase();
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (mime.startsWith('image/') || IMAGE_EXT.includes(ext)) return 'image';
  if (mime.startsWith('video/') || mime.startsWith('audio/') ||
      VIDEO_EXT.includes(ext) || AUDIO_EXT.includes(ext)) return 'video';
  return null;
}

function showNotice(message, type = 'info') {
  const node = el('notice');
  node.textContent = message;
  node.className = `notice show ${type}`;
}

function hideNotice() {
  el('notice').className = 'notice';
}

// --- Инструменты ------------------------------------------------------------
/** Инструмент, выбранный до загрузки файлов: задаёт действие и параметры. */
let pendingTool = null;

function applyPendingTool() {
  const tool = pendingTool;
  pendingTool = null;
  if (!tool || tool.kind !== state.kind) return;

  if (tool.kind === 'pdf') {
    // Склейка требует минимум двух файлов – иначе честно сжимаем.
    state.options.pdf.action = (tool.action === 'merge' && state.files.length < 2)
      ? 'compress'
      : tool.action;
    if (tool.action === 'merge' && state.files.length < 2) {
      showNotice('Для склейки нужно два файла и больше – включил сжатие.', 'info');
    }
  } else if (tool.kind === 'video') {
    state.options.video.action = tool.action;
  } else if (tool.kind === 'image' && tool.format) {
    state.options.image.format = tool.format;
  }
}

// --- Инспектор содержимого (клиентская сторона) ----------------------------
function inspectFiles(list) {
  const incoming = Array.from(list);
  const unknown = incoming.filter((file) => fileKind(file) === null);
  if (unknown.length) {
    showNotice(
      `Формат не поддерживается: ${unknown.map((f) => f.name).join(', ')}. ` +
      'Допустимы изображения, PDF и медиафайлы.',
      'error',
    );
    return false;
  }

  const kinds = new Set(incoming.map(fileKind));
  if (kinds.size > 1) {
    showNotice(
      'Пожалуйста, выберите файлы одного типа: смешивать изображения, PDF и видео нельзя.',
      'warn',
    );
    return false;
  }

  if (state.kind && state.kind !== [...kinds][0] && state.files.length) {
    showNotice('Сначала очистите список: типы файлов не должны смешиваться.', 'warn');
    return false;
  }

  const merged = state.files.concat(incoming);
  if (merged.length > 50) {
    showNotice('За один раз можно обработать не более 50 файлов.', 'error');
    return false;
  }
  const total = merged.reduce((sum, file) => sum + file.size, 0);
  if (total > 200 * 1024 * 1024) {
    showNotice('Суммарный вес файлов превышает 200 МБ.', 'error');
    return false;
  }

  state.files = merged;
  state.kind = [...kinds][0];
  applyPendingTool();
  hideNotice();
  render();
  return true;
}

// --- Отрисовка --------------------------------------------------------------
function idleDropzoneTitle() {
  return state.files.length ? 'Добавить файлы' : 'Перетащите файлы сюда';
}

/** Правильная форма слова «файл» для счётчика. */
function pluralFiles(count) {
  const tail = count % 100;
  if (tail >= 11 && tail <= 14) return 'файлов';
  const last = count % 10;
  if (last === 1) return 'файл';
  if (last >= 2 && last <= 4) return 'файла';
  return 'файлов';
}

/** Приводит зону загрузки к текущему состоянию: пустая или компактная полоса. */
function refreshDropzone() {
  const hasFiles = state.files.length > 0;
  el('dropzone').classList.toggle('compact', hasFiles);
  el('dropzone-title').textContent = idleDropzoneTitle();
}

function render() {
  const hasFiles = state.files.length > 0;
  el('config-card').classList.toggle('hidden', !hasFiles);
  el('result-card').classList.add('hidden');
  el('result-card').classList.remove('success');
  el('steps').classList.toggle('hidden', hasFiles);
  document.body.classList.toggle('has-files', hasFiles);
  refreshDropzone();
  document.querySelectorAll('.quick-card').forEach((card) => {
    card.classList.toggle('active', hasFiles && card.dataset.kind === state.kind);
  });
  if (!hasFiles) return;

  const count = el('file-count');
  if (count) count.textContent = String(state.files.length);

  const summary = el('files-summary');
  if (summary) {
    const total = state.files.reduce((sum, file) => sum + file.size, 0);
    const n = state.files.length;
    summary.textContent = `${n} ${pluralFiles(n)} – ${formatBytes(total)} всего`;
  }

  renderFileList();
  renderOptions();
}

function renderFileList() {
  const container = el('file-list');
  container.innerHTML = '';

  state.files.forEach((file, index) => {
    const row = document.createElement('div');
    row.className = 'file-row';
    row.draggable = state.kind === 'pdf' && state.files.length > 1;
    row.dataset.index = String(index);

    if (state.kind === 'image' && file.type.startsWith('image/')) {
      const thumb = document.createElement('img');
      thumb.className = 'file-thumb';
      thumb.alt = '';
      thumb.src = URL.createObjectURL(file);
      thumb.onload = () => URL.revokeObjectURL(thumb.src);
      row.appendChild(thumb);
    } else {
      const icon = document.createElement('div');
      icon.className = `file-thumb icon kind-${state.kind}`;
      icon.innerHTML = FILE_ICONS[state.kind] || FILE_ICONS.video;
      row.appendChild(icon);
    }

    const meta = document.createElement('div');
    meta.className = 'file-meta';
    const name = document.createElement('div');
    name.className = 'file-name';
    name.textContent = file.name;
    name.title = file.name;
    const size = document.createElement('div');
    size.className = 'file-size';
    size.textContent = formatBytes(file.size);
    meta.append(name, size);
    row.appendChild(meta);

    if (state.kind === 'pdf' && state.files.length > 1) {
      const handle = document.createElement('div');
      handle.className = 'drag-handle';
      handle.textContent = '⠿';
      handle.title = 'Перетащите, чтобы изменить порядок склейки';
      row.appendChild(handle);
      attachReorder(row);
    }

    const remove = document.createElement('button');
    remove.className = 'icon-button';
    remove.type = 'button';
    remove.textContent = '✕';
    remove.title = 'Убрать файл';
    remove.setAttribute('aria-label', `Убрать ${file.name}`);
    remove.onclick = () => {
      state.files.splice(index, 1);
      if (!state.files.length) {
        state.kind = null;
        el('config-card').classList.add('hidden');
        hideNotice();
      }
      render();  // пересчитывает счётчик и приводит зону загрузки в нужное состояние
    };
    row.appendChild(remove);
    container.appendChild(row);
  });
}

function attachReorder(row) {
  row.addEventListener('dragstart', (event) => {
    state.dragIndex = Number(row.dataset.index);
    row.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
  });
  row.addEventListener('dragend', () => {
    row.classList.remove('dragging');
    state.dragIndex = null;
  });
  row.addEventListener('dragover', (event) => {
    event.preventDefault();
    if (state.dragIndex === null) return;
    const target = Number(row.dataset.index);
    if (target === state.dragIndex) return;
    const [moved] = state.files.splice(state.dragIndex, 1);
    state.files.splice(target, 0, moved);
    state.dragIndex = target;
    renderFileList();
  });
}

function chip(label, active, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `chip${active ? ' active' : ''}`;
  button.textContent = label;
  button.setAttribute('role', 'radio');
  button.setAttribute('aria-checked', active ? 'true' : 'false');
  button.onclick = onClick;
  return button;
}

function field(labelText, node) {
  const wrap = document.createElement('div');
  wrap.className = 'field';
  const label = document.createElement('label');
  label.textContent = labelText;
  wrap.append(label, node);
  return wrap;
}

//: Пресеты для изображений: одним нажатием задают формат и качество.
const IMAGE_PRESETS = [
  { label: 'Для веба', format: 'webp', quality: 80 },
  { label: 'Баланс', format: 'jpg', quality: 85 },
  { label: 'Без потерь', format: 'png', quality: 100 },
];

function renderOptions() {
  const box = el('options');
  box.innerHTML = '';

  if (state.kind === 'image') {
    // Пресеты: одна кнопка вместо трёх решений – формат, качество, лимит.
    const presets = document.createElement('div');
    presets.className = 'presets';
    presets.setAttribute('role', 'group');
    presets.setAttribute('aria-label', 'Готовые наборы');
    [
      { label: 'Для почты', format: 'jpg', quality: 70, targetKb: 200 },
      { label: 'Telegram', format: 'jpg', quality: 82, targetKb: 0 },
      { label: 'Печать', format: 'png', quality: 100, targetKb: 0 },
      { label: 'С iPhone', format: 'jpg', quality: 85, targetKb: 0, maxWidth: 2000 },
    ].forEach((preset) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'preset';
      button.textContent = preset.label;
      const active = state.options.image.format === preset.format
        && state.options.image.quality === preset.quality
        && state.options.image.targetKb === preset.targetKb
        && (state.options.image.maxWidth || 0) === (preset.maxWidth || 0);
      button.classList.toggle('active', active);
      button.onclick = () => {
        state.options.image.format = preset.format;
        state.options.image.quality = preset.quality;
        state.options.image.targetKb = preset.targetKb;
        state.options.image.maxWidth = preset.maxWidth || 0;
        renderOptions();
        scheduleEstimate();
      };
      presets.appendChild(button);
    });
    box.appendChild(presets);

    const chips = document.createElement('div');
    chips.className = 'chips';
    chips.setAttribute('role', 'radiogroup');
    chips.setAttribute('aria-label', 'Целевой формат');
    ['jpg', 'png', 'webp'].forEach((format) => {
      chips.appendChild(chip(format.toUpperCase(), state.options.image.format === format, () => {
        state.options.image.format = format;
        renderOptions();
        scheduleEstimate();
      }));
    });
    box.appendChild(field('Формат', chips));

    const row = document.createElement('div');
    row.className = 'range-row';
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '10';
    slider.max = '100';
    slider.step = '1';
    slider.value = String(state.options.image.quality);
    slider.setAttribute('aria-label', 'Качество сжатия');
    const value = document.createElement('span');
    value.className = 'range-value';
    value.textContent = `${slider.value}%`;

    // Заполненная часть трека рисуется градиентом через CSS-переменную.
    const paint = () => {
      const min = Number(slider.min);
      const max = Number(slider.max);
      const percent = ((Number(slider.value) - min) / (max - min)) * 100;
      slider.style.setProperty('--pct', `${percent}%`);
    };
    paint();
    slider.oninput = () => {
      state.options.image.quality = Number(slider.value);
      value.textContent = `${slider.value}%`;
      const note = el('compare-note');
      if (note) note.textContent = `${state.options.image.format.toUpperCase()} · ${slider.value}%`;
      paint();
      scheduleEstimate();
    };
    row.append(slider, value);
    box.appendChild(field('Качество', row));

    if (state.options.image.format !== 'png') {
      const limit = document.createElement('input');
      limit.type = 'number';
      limit.min = '10';
      limit.max = '20000';
      limit.step = '10';
      limit.id = 'target-size';
      limit.placeholder = 'не задан';
      limit.value = state.options.image.targetKb ? String(state.options.image.targetKb) : '';
      limit.oninput = () => {
        const value = Number(limit.value);
        state.options.image.targetKb = Number.isFinite(value) && value > 0 ? Math.round(value) : 0;
      };
      box.appendChild(field('Не больше, КБ', limit));

      const widthInput = document.createElement('input');
      widthInput.type = 'number';
      widthInput.min = '16';
      widthInput.max = '10000';
      widthInput.step = '10';
      widthInput.id = 'target-width';
      widthInput.placeholder = 'исходная';
      widthInput.value = state.options.image.maxWidth ? String(state.options.image.maxWidth) : '';
      widthInput.oninput = () => {
        const value = Number(widthInput.value);
        state.options.image.maxWidth = Number.isFinite(value) && value > 0 ? Math.round(value) : 0;
      };
      box.appendChild(field('Ширина, px', widthInput));

      const strip = document.createElement('input');
      strip.type = 'checkbox';
      strip.id = 'strip-metadata';
      strip.className = 'switch';
      strip.checked = state.options.image.stripMetadata;
      strip.onchange = () => { state.options.image.stripMetadata = strip.checked; };
      box.appendChild(field('Убрать метаданные', strip));
    }

    const estimate = document.createElement('div');
    estimate.className = 'estimate';
    const estimateLabel = document.createElement('span');
    estimateLabel.textContent = 'Прогноз размера';
    const estimateValue = document.createElement('b');
    estimateValue.id = 'preview-estimate';
    estimateValue.textContent = '—';
    estimate.append(estimateLabel, estimateValue);
    box.appendChild(estimate);
    scheduleEstimate();

    if (state.files.length > 1) {
      showNotice('Несколько изображений будут упакованы в ZIP-архив.', 'info');
    } else {
      hideNotice();
    }
    return;
  }

  if (state.kind === 'pdf') {
    const single = state.files.length === 1;
    const chips = document.createElement('div');
    chips.className = 'chips';
    chips.setAttribute('role', 'radiogroup');

    if (single) {
      chips.appendChild(chip('Сжать', state.options.pdf.action === 'compress', () => {
        state.options.pdf.action = 'compress';
        renderOptions();
      }));
      chips.appendChild(chip('В картинки', state.options.pdf.action === 'to_images', () => {
        state.options.pdf.action = 'to_images';
        renderOptions();
      }));
    } else {
      state.options.pdf.action = 'merge';
      chips.appendChild(chip('Склеить', true, () => {}));
    }
    box.appendChild(field('Действие', chips));

    if (state.options.pdf.action === 'to_images') {
      const dpiChips = document.createElement('div');
      dpiChips.className = 'chips';
      dpiChips.setAttribute('role', 'radiogroup');
      [150, 200].forEach((dpi) => {
        dpiChips.appendChild(chip(`${dpi} DPI`, state.options.pdf.dpi === dpi, () => {
          state.options.pdf.dpi = dpi;
          renderOptions();
        }));
      });
      box.appendChild(field('Плотность', dpiChips));
      showNotice('Каждая страница станет отдельным PNG и будет упакована в ZIP.', 'info');
    } else if (!single) {
      showNotice('Порядок файлов определяет порядок склейки. Перетаскивайте строки.', 'info');
    } else {
      hideNotice();
    }
    return;
  }

  // Видео и аудио
  const chips = document.createElement('div');
  chips.className = 'chips';
  chips.setAttribute('role', 'radiogroup');
  const actions = [
    ['mp3', 'Звук (MP3)'],
    ['gif', 'GIF'],
    ['compress', 'Сжать видео'],
  ];
  actions.forEach(([value, label]) => {
    chips.appendChild(chip(label, state.options.video.action === value, () => {
      state.options.video.action = value;
      renderOptions();
    }));
  });
  box.appendChild(field('Действие', chips));

  if (state.options.video.action === 'mp3') {
    const select = document.createElement('select');
    select.setAttribute('aria-label', 'Битрейт MP3');
    [128, 192, 256, 320].forEach((rate) => {
      const option = document.createElement('option');
      option.value = String(rate);
      option.textContent = `${rate} kbps`;
      if (state.options.video.bitrate === rate) option.selected = true;
      select.appendChild(option);
    });
    select.onchange = () => { state.options.video.bitrate = Number(select.value); };
    box.appendChild(field('Битрейт (44.1 кГц)', select));
  } else if (state.options.video.action === 'gif') {
    const grid = document.createElement('div');
    grid.className = 'grid-2';
    const numberField = (key, min, max, label) => {
      const input = document.createElement('input');
      input.type = 'number';
      input.min = String(min);
      input.max = String(max);
      input.value = String(state.options.video[key]);
      input.oninput = () => { state.options.video[key] = Number(input.value); };
      return field(label, input);
    };
    grid.append(
      numberField('gifSeconds', 1, 60, 'Длительность, с'),
      numberField('gifFps', 5, 30, 'Кадров в секунду'),
      numberField('gifWidth', 120, 1280, 'Ширина, px'),
    );
    box.appendChild(grid);
  } else {
    const select = document.createElement('select');
    select.setAttribute('aria-label', 'Степень сжатия');
    [[27, 'CRF 27'], [26, 'CRF 26'], [28, 'CRF 28']]
      .forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = String(value);
        option.textContent = label;
        if (state.options.video.crf === value) option.selected = true;
        select.appendChild(option);
      });
    select.onchange = () => { state.options.video.crf = Number(select.value); };
    box.appendChild(field('Степень сжатия', select));
  }

  if (!state.ffmpeg.available) {
    showNotice('FFmpeg не найден на сервере – обработка видео и аудио вернёт ошибку 503. ' +
               'Установите FFmpeg и перезапустите сервер.', 'warn');
  } else if (state.options.video.action === 'gif') {
    showNotice('Обрабатываются только первые секунды видео – так GIF остаётся лёгким.', 'info');
  } else {
    hideNotice();
  }
}

// --- Прогноз размера --------------------------------------------------------
/**
 * Пересчитывает первый файл через canvas с текущими форматом и качеством.
 * Это настоящая оценка, а не догадка: браузер кодирует кадр тем же кодеком,
 * которым пользуется сервер.
 */
let previewSeq = 0;

function scheduleEstimate() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(updateEstimate, 160);
}

let beforeUrl = null;

/** Левая миниатюра – исходный файл. */
function renderBeforeThumb() {
  const slot = el('compare-before');
  if (!slot || state.kind !== 'image' || !state.files.length) return;
  if (beforeUrl) URL.revokeObjectURL(beforeUrl);
  beforeUrl = URL.createObjectURL(state.files[0]);
  slot.innerHTML = '';
  const image = document.createElement('img');
  image.alt = '';
  image.src = beforeUrl;
  slot.appendChild(image);
}

function updateEstimate() {
  const sizeNode = el('preview-estimate');
  if (!sizeNode) return;
  if (state.kind !== 'image' || !state.files.length) {
    sizeNode.textContent = '≈ —';
    return;
  }
  const format = state.options.image.format;
  const mime = { jpg: 'image/jpeg', png: 'image/png', webp: 'image/webp' }[format];
  if (!mime) return;

  const seq = ++previewSeq;
  const image = document.createElement('img');
  image.onload = () => {
    try {
      const canvas = document.createElement('canvas');
      if (typeof canvas.getContext !== 'function') return;
      const maxSide = 1200;
      const scale = Math.min(1, maxSide / Math.max(image.naturalWidth || 1, image.naturalHeight || 1));
      canvas.width = Math.max(1, Math.round((image.naturalWidth || 1) * scale));
      canvas.height = Math.max(1, Math.round((image.naturalHeight || 1) * scale));
      const context = canvas.getContext('2d');
      if (!context) return;
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const quality = format === 'png' ? undefined : state.options.image.quality / 100;
      canvas.toBlob((blob) => {
        if (seq !== previewSeq || !blob) return;
        sizeNode.textContent = `≈ ${formatBytes(blob.size)}`;
        // Честно показываем, если выбранные параметры делают файл больше исходного.
        sizeNode.classList.toggle('worse', blob.size > state.files[0].size);
      }, mime, quality);
    } catch (error) {
      node.textContent = '—';
    }
  };
  image.onerror = () => { node.textContent = '—'; };
  image.src = URL.createObjectURL(state.files[0]);
}

// --- Прогресс ---------------------------------------------------------------
function setProgress(percent, text, indeterminate = false) {
  el('progress-wrap').classList.add('show');
  const bar = el('progress-bar');
  bar.classList.toggle('indeterminate', indeterminate);
  if (!indeterminate) bar.style.width = `${Math.max(2, Math.min(100, percent))}%`;
  el('progress-text').textContent = text;
}

function clearProgress() {
  el('progress-wrap').classList.remove('show');
  el('progress-bar').style.width = '0';
  el('progress-bar').classList.remove('indeterminate');
}

function setBusy(busy) {
  el('convert-button').disabled = busy;
  el('clear-button').disabled = busy;
  el('convert-button').textContent = busy ? 'Обработка…' : 'Обработать';
}

// --- Отправка задачи --------------------------------------------------------
function buildOptions() {
  if (state.kind === 'image') {
    return {
      format: state.options.image.format,
      quality: state.options.image.quality,
      target_size_kb: state.options.image.targetKb || '',
      width: state.options.image.maxWidth || '',
      strip_metadata: state.options.image.stripMetadata ? 'true' : 'false',
    };
  }
  if (state.kind === 'pdf') {
    const options = { action: state.options.pdf.action };
    if (state.options.pdf.action === 'to_images') options.dpi = state.options.pdf.dpi;
    return options;
  }
  const video = state.options.video;
  if (video.action === 'mp3') return { action: 'mp3', bitrate: video.bitrate };
  if (video.action === 'gif') {
    return { action: 'gif', fps: video.gifFps, width: video.gifWidth, seconds: video.gifSeconds };
  }
  return { action: 'compress', crf: video.crf };
}

async function submit() {
  const form = new FormData();
  form.append('kind', state.kind);
  const options = buildOptions();
  Object.entries(options).forEach(([key, value]) => form.append(key, String(value)));
  state.files.forEach((file) => form.append('files', file, file.name));

  setBusy(true);
  clearProgress();
  hideNotice();
  const retry = el('retry-button');
  if (retry) retry.classList.add('hidden');
  setProgress(0, 'Загрузка на сервер…');

  try {
    const response = await fetch('/api/convert', { method: 'POST', body: form });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `Сервер ответил кодом ${response.status}`);
    }
    state.jobId = payload.job_id;
    queuePush(state.files.length > 1 ? `${state.files.length} файла` : state.files[0].name);
    setProgress(4, 'Файлы переданы, начинается обработка…', true);
    pollJob(payload.job_id);
  } catch (error) {
    setBusy(false);
    clearProgress();
    showNotice(error.message, 'error');
    if (retry) retry.classList.remove('hidden');
  }
}

function pollJob(jobId) {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (!response.ok) throw new Error(job.message || 'Не удалось получить статус задачи');
      state.job = job;

      if (job.status === 'queued' || job.status === 'processing') {
        setProgress(job.progress || 3, job.stage || 'Выполняется конвертация…');
        queueProgress(job.progress || 3);
        return;
      }

      clearInterval(state.polling);
      setBusy(false);
      state.jobId = null;

      if (job.status === 'done') {
        clearProgress();
        renderResult(job);
      } else {
        clearProgress();
        showNotice((job.error && job.error.message) || 'Обработка завершилась ошибкой', 'error');
      }
    } catch (error) {
      clearInterval(state.polling);
      setBusy(false);
      clearProgress();
      showNotice(error.message, 'error');
    }
  }, 700);
}

// --- Карточка результата ----------------------------------------------------
/** Длительность в человеческом виде: «0,4 с» или «1 мин 12 с». */
function formatDuration(seconds) {
  if (!seconds || seconds < 0) return null;
  if (seconds < 60) {
    const value = seconds < 10 ? seconds.toFixed(1) : Math.round(seconds).toString();
    return `${value.replace('.', ',')} с`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes} мин ${rest} с`;
}

/** Плавно доводит число в плашке экономии до итогового значения. */
function animateSavings(node, value) {
  const reduce = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) {
    node.textContent = `−${value}%`;
    return;
  }
  const raf = (window.requestAnimationFrame
    || ((callback) => setTimeout(() => callback(performance.now()), 16)));
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / 420);
    const eased = 1 - Math.pow(1 - progress, 3);
    node.textContent = `−${(value * eased).toFixed(1).replace('.', ',')}%`;
    if (progress < 1) raf(step);
    else node.textContent = `−${String(value).replace('.', ',')}%`;
  };
  raf(step);
}

function renderResult(job) {
  const result = job.result || {};
  const saved = result.savings_percent || 0;

  const metrics = el('metrics');
  metrics.innerHTML = `
    <div class="metric">
      <span class="metric-label">Было</span>
      <span class="metric-value">${formatBytes(result.input_bytes)}</span>
    </div>
    <span class="metric-arrow">→</span>
    <div class="metric">
      <span class="metric-label">Стало</span>
      <span class="metric-value">${formatBytes(result.output_bytes)}</span>
    </div>
    ${saved > 0 ? '<span class="metric-saved">−0%</span>' : ''}
  `;

  const pill = metrics.querySelector('.metric-saved');
  if (pill && saved > 0) animateSavings(pill, saved);

  // Строка деталей: время обработки, освободившийся объём, число файлов.
  const meta = el('result-meta');
  if (meta) {
    const parts = [];
    const duration = formatDuration(job.duration_seconds);
    if (duration) parts.push(`готово за ${duration}`);
    const freed = (result.input_bytes || 0) - (result.output_bytes || 0);
    if (freed > 0) parts.push(`освободилось ${formatBytes(freed)}`);
    if (result.file_count) parts.push(`файлов: ${result.file_count}`);
    meta.textContent = parts.join(' – ');
  }

  const downloads = el('downloads');
  downloads.innerHTML = '';
  const link = document.createElement('a');
  link.className = 'download-link';
  link.href = job.download_url;
  link.download = result.download_name || '';
  link.innerHTML = `<span>${result.download_name || 'Скачать результат'}</span>
                    <span class="muted">${formatBytes(result.output_bytes)}</span>`;
  downloads.appendChild(link);

  const warnings = el('warnings');
  warnings.innerHTML = '';
  (result.warnings || []).forEach((text) => {
    const node = document.createElement('div');
    node.className = 'notice show warn';
    node.style.marginTop = '16px';
    node.textContent = text;
    warnings.appendChild(node);
  });

  rememberQueue();
  el('result-card').classList.add('success');
  el('result-card').classList.remove('hidden');
  el('result-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// --- Демонстрационный режим -------------------------------------------------
/**
 * `?demo=1` подставляет пример изображения, чтобы посмотреть интерфейс
 * в рабочем состоянии, ничего не перетаскивая. Полезно для показа и проверок.
 */
async function loadDemoFiles() {
  const search = (window.location && window.location.search) || '';
  const params = new URLSearchParams(search);
  if (!params.has('demo')) return;
  try {
    const response = await fetch('/static/demo-image.png');
    if (!response.ok) return;
    const blob = await response.blob();
    const file = new File([blob], 'пример-фото.png', { type: 'image/png' });
    inspectFiles([file]);
    showNotice('Демонстрационный режим: подставлен пример изображения.', 'info');
  } catch (error) {
    showNotice('Не удалось загрузить пример изображения для демо-режима.', 'warn');
  }
}

// --- QR-мост ----------------------------------------------------------------
async function openQr() {
  if (!state.job) return;
  const url = state.job.qr_target || `${state.network.base_url}/download/${state.job.task_id}`;
  el('qr-image').src = state.job.qr_url;
  el('qr-url').textContent = url;
  el('qr-modal').classList.add('show');
}

function closeQr() {
  el('qr-modal').classList.remove('show');
}

function resetAll() {
  state.files = [];
  state.kind = null;
  state.job = null;
  clearInterval(state.polling);
  hideNotice();
  clearProgress();
  setBusy(false);
  el('file-input').value = '';
  el('result-card').classList.add('hidden');
  el('result-card').classList.remove('success');
  el('config-card').classList.add('hidden');
  el('steps').classList.remove('hidden');
  refreshDropzone();
  document.querySelectorAll('.quick-card').forEach((card) => card.classList.remove('active'));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// --- Служебные запросы ------------------------------------------------------
async function loadStatus() {
  try {
    const response = await fetch('/api/health');
    const health = await response.json();
    state.network = health.network || {};
    state.ffmpeg = health.ffmpeg || { available: false };
    const netInfo = el('net-info');
    if (netInfo) netInfo.textContent = `${state.network.local_ip || '–'}:${state.network.port || ''}`;

    const badge = el('status-badge');
    const ready = Boolean(health.ffmpeg && health.ffmpeg.available);
    const statusText = ready ? 'сервер готов' : 'ffmpeg не найден';
    badge.title = statusText;
    badge.setAttribute('aria-label', statusText);
    badge.classList.toggle('warn', !ready);
    badge.textContent = '';
  } catch (error) {
    const offline = el('status-badge');
    offline.title = 'Сервер недоступен';
    offline.setAttribute('aria-label', 'Сервер недоступен');
    offline.classList.add('warn');
  }
}

// --- Привязка событий -------------------------------------------------------
function setup() {
  const dropzone = el('dropzone');
  const input = el('file-input');
  // Клик по скрытому input не должен всплывать до зоны приёма и открывать окно дважды.
  input.addEventListener('click', (event) => event.stopPropagation());

  dropzone.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    input.accept = '';
    input.click();
  });

  dropzone.addEventListener('click', () => {
    input.accept = '';
    input.click();
  });

  const browse = el('browse-button');
  if (browse) {
    browse.addEventListener('click', (event) => {
      event.stopPropagation();
      input.click();
    });
  }

  input.addEventListener('change', () => {
    if (input.files && input.files.length) inspectFiles(input.files);
    input.value = '';
  });

  ['dragenter', 'dragover'].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add('dragover');
      document.body.classList.add('is-dragging');
      const dragged = event.dataTransfer && event.dataTransfer.items
        ? event.dataTransfer.items.length
        : 0;
      el('dropzone-title').textContent = dragged
        ? `Отпустите – ${dragged} ${pluralFiles(dragged)}`
        : 'Отпустите файлы';
    });
  });

  ['dragleave', 'dragend'].forEach((type) => {
    dropzone.addEventListener(type, () => {
      dropzone.classList.remove('dragover');
      document.body.classList.remove('is-dragging');
      refreshDropzone();
    });
  });

  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.remove('dragover');
    document.body.classList.remove('is-dragging');
    refreshDropzone();
    if (event.dataTransfer && event.dataTransfer.files.length) {
      inspectFiles(event.dataTransfer.files);
    }
  });

  // Запрещаем браузеру открывать файл при промахе мимо зоны.
  ['dragover', 'drop'].forEach((type) => {
    window.addEventListener(type, (event) => event.preventDefault());
  });

  // Ctrl/⌘ + Enter – запуск обработки без мыши.
  document.addEventListener('keydown', (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.key !== 'Enter') return;
    const button = el('convert-button');
    if (!button || button.disabled || !state.files.length) return;
    event.preventDefault();
    submit();
  });

  // Стрелками можно переключать сегментированный контрол, как в системных настройках.
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const target = event.target;
    if (!target || typeof target.closest !== 'function') return;
    const chip = target.closest('.chip');
    if (!chip || !chip.parentElement) return;
    const siblings = Array.from(chip.parentElement.querySelectorAll('.chip'));
    const index = siblings.indexOf(chip);
    if (index < 0) return;
    const step = event.key === 'ArrowRight' ? 1 : -1;
    const next = siblings[(index + step + siblings.length) % siblings.length];
    if (!next) return;
    event.preventDefault();
    next.focus();
    next.click();
  });

  // Карточки быстрого старта: сразу открывают выбор файлов нужного типа.
  const quickKinds = {
    'quick-images': 'image/*',
    'quick-pdf': 'application/pdf',
    'quick-video': 'video/*,audio/*',
  };
  Object.entries(quickKinds).forEach(([id, accept]) => {
    const card = el(id);
    if (!card) return;
    card.addEventListener('click', () => {
      input.accept = accept;
      input.click();
    });
  });

  // Инструменты: заранее выбирают действие под выбранные файлы.
  const tools = {
    'tool-pdf-images': { accept: 'application/pdf', kind: 'pdf', action: 'to_images' },
    'tool-pdf-merge': { accept: 'application/pdf', kind: 'pdf', action: 'merge' },
    'tool-pdf-compress': { accept: 'application/pdf', kind: 'pdf', action: 'compress' },
    'tool-video-mp3': { accept: 'video/*,audio/*', kind: 'video', action: 'mp3' },
    'tool-video-gif': { accept: 'video/*', kind: 'video', action: 'gif' },
    'tool-video-compress': { accept: 'video/*', kind: 'video', action: 'compress' },
  };
  Object.entries(tools).forEach(([id, tool]) => {
    const card = el(id);
    if (!card) return;
    card.addEventListener('click', () => {
      pendingTool = tool;
      input.accept = tool.accept;
      input.click();
    });
  });

  const navAdd = el('nav-add');
  if (navAdd) {
    navAdd.addEventListener('click', () => {
      input.accept = '';
      input.click();
    });
  }

  // Копирование ссылки на результат – как в CloudConvert.
  const copyButton = el('copy-link');
  if (copyButton) {
    copyButton.addEventListener('click', async () => {
      const link = el('downloads') && el('downloads').querySelector('a');
      if (!link) return;
      const url = new URL(link.getAttribute('href'), window.location.href).href;
      try {
        await navigator.clipboard.writeText(url);
        copyButton.textContent = 'Скопировано';
        setTimeout(() => { copyButton.textContent = 'Скопировать ссылку'; }, 1600);
      } catch (error) {
        showNotice('Браузер не дал скопировать – ссылку можно открыть вручную.', 'info');
      }
    });
  }

  // Вставка из буфера обмена – как в Squoosh: Ctrl/⌘ + V добавляет картинку.
  document.addEventListener('paste', (event) => {
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    const pasted = [];
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (file) pasted.push(file);
    }
    if (!pasted.length) return;
    event.preventDefault();
    inspectFiles(pasted);
  });

  const cancelButton = el('cancel-button');
  if (cancelButton) {
    cancelButton.addEventListener('click', async () => {
      if (!state.jobId) return;
      const id = state.jobId;
      state.jobId = null;
      clearInterval(state.polling);
      setBusy(false);
      clearProgress();
      try {
        await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
        showNotice('Обработка отменена.', 'info');
      } catch (error) {
        showNotice('Не удалось отменить – сервер не ответил.', 'warn');
      }
    });
  }

  // Приём файлов с телефона: QR ведёт на страницу отправки, результат забираем сами.
  let sendTaskId = null;
  let sendPoll = null;

  async function pullInbox() {
    if (!sendTaskId) return;
    try {
      const response = await fetch(`/api/inbox/${sendTaskId}`);
      const data = await response.json();
      if (!data.files || !data.files.length) return;
      clearInterval(sendPoll);
      const arrived = [];
      for (const name of data.files) {
        const blob = await (await fetch(`/api/inbox/${sendTaskId}/${encodeURIComponent(name)}`)).blob();
        arrived.push(new File([blob], name, { type: blob.type || 'application/octet-stream' }));
      }
      el('qr-modal').classList.remove('show');
      sendTaskId = null;
      inspectFiles(arrived);
    } catch (error) { /* тихо: телефон ещё не отправил */ }
  }

  const sendButton = el('send-button');
  if (sendButton) {
    sendButton.addEventListener('click', async (event) => {
      event.stopPropagation();
      try {
        const response = await fetch('/api/send', { method: 'POST' });
        const data = await response.json();
        sendTaskId = data.task_id;
        el('qr-image').src = `/qr/send/${sendTaskId}`;
        el('qr-url').textContent = data.url;
        el('qr-modal').classList.add('show');
        clearInterval(sendPoll);
        sendPoll = setInterval(pullInbox, 2000);
      } catch (error) {
        showNotice('Не удалось создать сессию приёма.', 'warn');
      }
    });
  }

  // Перетаскивание в любое место страницы: промах по зоне не должен мешать.
  document.addEventListener('dragover', (event) => {
    event.preventDefault();
    document.body.classList.add('is-dragging');
  });

  document.addEventListener('dragleave', (event) => {
    if (!event.relatedTarget) document.body.classList.remove('is-dragging');
  });

  document.addEventListener('drop', (event) => {
    event.preventDefault();
    document.body.classList.remove('is-dragging');
    if (event.target && typeof event.target.closest === 'function' && event.target.closest('#dropzone')) {
      return; // зона приёма обработает сама
    }
    if (event.dataTransfer && event.dataTransfer.files.length) {
      inspectFiles(event.dataTransfer.files);
    }
  });

  const retryButton = el('retry-button');
  if (retryButton) {
    retryButton.addEventListener('click', () => submit());
  }

  el('convert-button').addEventListener('click', submit);
  el('clear-button').addEventListener('click', resetAll);
  el('reset-button').addEventListener('click', resetAll);
  el('download-button').addEventListener('click', () => {
    if (!state.job) return;
    const link = document.createElement('a');
    link.href = state.job.download_url;
    if (state.job.result) link.download = state.job.result.download_name;
    document.body.appendChild(link);
    link.click();
    link.remove();
  });
  el('qr-button').addEventListener('click', openQr);
  el('qr-close').addEventListener('click', closeQr);
  el('qr-modal').addEventListener('click', (event) => {
    if (event.target === el('qr-modal')) closeQr();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeQr();
  });

  loadStatus();
  loadDemoFiles();
}

document.addEventListener('DOMContentLoaded', setup);

// --- Тема -------------------------------------------------------------------
const THEMES = ['system', 'dark', 'light'];
const THEME_KEY = 'mediahub-theme';

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'system') {
    root.removeAttribute('data-theme');
    root.style.colorScheme = '';
  } else {
    root.setAttribute('data-theme', mode);
    root.style.colorScheme = mode;
  }
  try { localStorage.setItem(THEME_KEY, mode); } catch (error) { /* приватный режим */ }
  const button = el('theme-toggle');
  if (button) {
    button.textContent = mode === 'dark' ? '☾' : mode === 'light' ? '☀' : '◐';
    button.title = mode === 'dark' ? 'Тёмная тема' : mode === 'light' ? 'Светлая тема' : 'Как в системе';
  }
}

function initTheme() {
  let mode = 'system';
  try { mode = localStorage.getItem(THEME_KEY) || 'system'; } catch (error) { /* нет доступа */ }
  applyTheme(mode);
  const button = el('theme-toggle');
  if (!button) return;
  button.addEventListener('click', () => {
    let current = 'system';
    try { current = localStorage.getItem(THEME_KEY) || 'system'; } catch (error) { /* нет доступа */ }
    applyTheme(THEMES[(THEMES.indexOf(current) + 1) % THEMES.length]);
  });
}

// --- Очередь сессии ---------------------------------------------------------
function queuePush(name) {
  state.queue = state.queue || [];
  state.queue.unshift({ name, status: 'в работе', href: '' });
  state.queue = state.queue.slice(0, 6);
  renderQueue();
}

function queueProgress(percent) {
  const item = (state.queue || [])[0];
  if (!item || item.status === 'готово') return;
  item.status = `${Math.max(1, Math.round(percent || 0))}%`;
  renderQueue();
}

function queueFinish(href, status) {
  const item = (state.queue || [])[0];
  if (!item) return;
  item.status = status;
  if (href) item.href = href;
  renderQueue();
}

function rememberQueue() {
  const link = el('downloads') && el('downloads').querySelector('a');
  queueFinish(link ? link.getAttribute('href') : '', 'готово');
  const item = (state.queue || [])[0];
  if (item && link) item.name = link.textContent.trim();
}

function renderQueue() {
  const section = el('queue');
  const list = el('queue-list');
  if (!section || !list) return;
  list.innerHTML = '';
  (state.queue || []).forEach((item) => {
    const row = document.createElement('div');
    row.className = 'queue-row';
    const name = document.createElement('span');
    name.className = 'q-name';
    name.textContent = item.name;
    const status = document.createElement('span');
    status.className = 'q-status';
    status.textContent = item.status || 'готово';
    row.append(name, status);
    if (item.href) {
      const download = document.createElement('a');
      download.href = item.href;
      download.textContent = 'скачать';
      row.append(download);
    }
    list.appendChild(row);
  });
  section.classList.toggle('hidden', !(state.queue || []).length);
}
// --- PWA --------------------------------------------------------------------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  });
}