/* DNX Background v1 — capa de aplicación + tarjeta de Ajustes.
 *
 * - DnxBg.apply(meta)  pinta el fondo en caliente (CSS) y lo recuerda.
 * - DnxBg.load()       lee GET /api/v1/branding (público; sirve al login).
 * - DnxBg.card(meta)   HTML de la tarjeta «Fondo de la aplicación».
 * - DnxBg.bind(root, meta)  conecta la tarjeta (imagen, previews, guardar).
 *
 * El procesado pesado ocurre SOLO aquí, al elegir la imagen. El servidor
 * guarda el resultado y el resto de clientes reciben el mismo fichero.
 */
(function (root) {
  'use strict';

  var LS_KEY = 'dnx_bg';
  var MAX_FILE = 12 * 1024 * 1024;
  var RECOMMENDED = { mask: 6, color: 20, original: 100 };
  var STATE = { instance_name: '', background: null };

  function num(v, def) {
    var n = Number(v);
    return isFinite(n) ? n : def;
  }

  function bgUrl(sha) {
    return '/branding/bg?v=' + encodeURIComponent(String(sha || ''));
  }

  function persist(meta) {
    try {
      if (meta && meta.mode && meta.mode !== 'none' && meta.sha) {
        localStorage.setItem(LS_KEY, JSON.stringify(meta));
      } else {
        localStorage.removeItem(LS_KEY);
      }
    } catch (e) { /* localStorage bloqueado */ }
  }

  /* Aplica un fondo (o lo quita si mode = none). Solo toca CSS. */
  function apply(meta) {
    var el = document.documentElement;
    var m = meta && meta.mode && meta.mode !== 'none' && meta.sha ? meta : null;
    if (!m) {
      el.removeAttribute('data-dnx-bg');
      el.style.removeProperty('--dnx-bg-url');
      el.style.setProperty('--dnx-bg-intensity', '0');
      el.style.removeProperty('--dnx-bg-tint');
      persist(null);
      return null;
    }
    var intensity = Math.max(0, Math.min(100, num(m.intensity, 6)));
    el.setAttribute('data-dnx-bg', m.mode);
    el.style.setProperty('--dnx-bg-url', 'url("' + bgUrl(m.sha) + '")');
    el.style.setProperty('--dnx-bg-intensity', String(intensity / 100));
    if (m.tint && m.tint !== 'auto' && /^#[0-9a-f]{6}$/i.test(m.tint)) {
      el.style.setProperty('--dnx-bg-tint', m.tint);
    } else {
      el.style.removeProperty('--dnx-bg-tint');
    }
    persist({ mode: m.mode, intensity: intensity, tint: m.tint || 'auto', sha: m.sha });
    return m;
  }

  /* Fondo público: también se usa antes de iniciar sesión. */
  async function load() {
    try {
      var r = await fetch('/api/v1/branding', { cache: 'no-store' });
      if (!r.ok) return null;
      var d = await r.json();
      STATE.instance_name = d.instance_name || '';
      STATE.background = d.background || null;
      apply(STATE.background);
      return STATE;
    } catch (e) {
      return null;
    }
  }

  /* ── tarjeta ─────────────────────────────────────────────────────────── */

  function card(meta) {
    meta = meta || { mode: 'none' };
    var b = meta.background && meta.background.mode ? meta.background : meta;
    var intensity = num(b.intensity, 6);
    var tint = b.tint || 'auto';
    var p = b.params || {};
    var saved = b.mode && b.mode !== 'none' && b.sha;
    var keyShown = /^#[0-9a-f]{6}$/i.test(String(b.key || '')) ? b.key : '#ffffff';
    var label = saved
      ? ({ mask: 'Máscara', color: 'Color con alfa', original: 'Original' }[b.mode] || b.mode)
      : 'Sin fondo';

    return '<div class="card mb-12" id="bg-card">' +
      '<div class="card-header"><h2>Fondo de la aplicación</h2>' +
        '<span class="badge ' + (saved ? 'badge-ok' : 'badge-muted') + '" id="bg-state">' +
          esc(label) + '</span></div>' +
      '<p class="muted small mt-4">Una textura propia de esta instalación (visible también en el ' +
        'login). Se procesa una sola vez en este navegador: el servidor guarda el resultado y ' +
        'todos los clientes reciben la misma imagen.</p>' +
      '<div class="grid g2 mt-12">' +
        '<div class="field"><label>Imagen</label>' +
          '<div class="bg-drop" id="bg-drop">' +
            '<input type="file" id="bg-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>' +
            '<button type="button" class="btn btn-ghost btn-sm" id="bg-pick">Elegir imagen…</button>' +
            '<div class="muted small mt-8">o arrástrala aquí</div>' +
          '</div>' +
          '<div class="hint">PNG, JPEG, WebP o GIF · máx. 12 MB. Nunca se sube el original: ' +
            'solo el resultado ya recortado (≤ 2048 px).</div>' +
        '</div>' +
        '<div class="field"><label>Previsualización</label>' +
          '<div class="bg-previews">' +
            '<div class="bg-prev bg-prev-dark" id="bg-prev-dark">' +
              '<div class="bg-prev-layer"></div><span>Oscuro</span></div>' +
            '<div class="bg-prev bg-prev-light" id="bg-prev-light">' +
              '<div class="bg-prev-layer"></div><span>Claro</span></div>' +
          '</div>' +
          '<div class="hint" id="bg-detect">—</div>' +
        '</div>' +
      '</div>' +
      '<div class="grid g3 mt-12">' +
        '<div class="field"><label>Salida</label>' +
          '<select id="bg-out">' +
            '<option value="auto">Auto (recomendado)</option>' +
            '<option value="mask">Máscara tintable</option>' +
            '<option value="color">Color con alfa</option>' +
            '<option value="original">Original</option>' +
          '</select>' +
          '<div class="hint">Auto elige según el análisis de la imagen.</div></div>' +
        '<div class="field"><label>Intensidad <span id="bg-int-val" class="muted"></span></label>' +
          '<input id="bg-int" type="range" min="0" max="100" step="1" value="' + intensity + '"></div>' +
        '<div class="field"><label>Suavizado <span id="bg-blur-val" class="muted"></span></label>' +
          '<input id="bg-blur" type="range" min="0" max="10" step="1" value="' +
            num(p.blur, 2) + '">' +
          '<div class="hint">Va dentro de la imagen; cambiarlo obliga a generarla otra vez.</div></div>' +
      '</div>' +
      '<details class="bg-adv" id="bg-adv"><summary>Avanzado</summary>' +
        '<div class="bg-adv-body">' +
          '<div class="grid g2">' +
            '<div class="field"><label>Tinte de la máscara</label>' +
              '<div class="bg-keyrow">' +
                '<input type="color" id="bg-tint" value="' +
                  (tint !== 'auto' ? esc(tint) : '#e8eef6') + '">' +
                '<label class="checkline"><input type="checkbox" id="bg-tint-auto"' +
                  (tint === 'auto' ? ' checked' : '') + '> Auto (color del tema)</label>' +
              '</div>' +
              '<div class="hint">Con «Auto» el mismo fichero funciona en claro y oscuro.</div></div>' +
            '<div class="field"><label>Color clave</label>' +
              '<div class="bg-keyrow">' +
                '<input type="color" id="bg-key" value="' + esc(keyShown) + '">' +
                '<input type="text" id="bg-key-hex" class="mono input-edit" value="' +
                  esc(b.key || 'auto') + '">' +
                '<button type="button" class="btn btn-ghost btn-sm" id="bg-key-auto">Auto</button>' +
              '</div>' +
              '<div class="hint">El color que se hace transparente. «Auto» lo detecta en el borde.</div></div>' +
            '<div class="field" id="bg-tol-wrap"><label>Tolerancia <span id="bg-tol-val" class="muted"></span></label>' +
              '<input id="bg-tol" type="range" min="0" max="100" step="1" value="' +
                num(p.tolerance, 12) + '"></div>' +
            '<div class="field" id="bg-feather-wrap"><label>Borde suave <span id="bg-feather-val" class="muted"></span></label>' +
              '<input id="bg-feather" type="range" min="0" max="100" step="1" value="' +
                num(p.feather, 8) + '"></div>' +
            '<div class="field" id="bg-mode-wrap"><label>Forzar detección</label>' +
              '<select id="bg-mode">' +
                '<option value="auto">Auto</option>' +
                '<option value="chroma">Croma (color)</option>' +
                '<option value="luma">Luma (grises)</option>' +
              '</select></div>' +
            '<div class="field"><label>Opciones</label>' +
              '<label class="checkline"><input type="checkbox" id="bg-despill"' +
                (p.despill === false ? '' : ' checked') + '> Quitar halo del borde</label>' +
              '<label class="checkline"><input type="checkbox" id="bg-invert"' +
                (p.invert ? ' checked' : '') + '> Invertir transparencia</label></div>' +
          '</div>' +
          '<div class="field mt-12"><label>Cuentagotas (clic sobre la imagen)</label>' +
            '<div class="bg-pick-scroll" id="bg-pick-scroll"><canvas id="bg-pick-canvas"></canvas></div>' +
            '<div class="bg-keyrow">' +
              '<span class="muted small">Zoom</span>' +
              '<input id="bg-pick-zoom" type="range" min="1" max="4" step="0.25" value="1">' +
              '<span class="muted small">Ajusta el zoom y desplaza para acertar el píxel.</span>' +
            '</div></div>' +
        '</div>' +
      '</details>' +
      '<div class="flex gap-8 mt-12" style="align-items:center">' +
        '<button type="button" class="btn btn-primary btn-sm" id="bg-save">Guardar fondo</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" id="bg-clear"' +
          (saved ? '' : ' disabled') + '>Quitar</button>' +
        '<span class="muted small" id="bg-msg"></span>' +
      '</div>' +
    '</div>';
  }

  /* ── comportamiento de la tarjeta ────────────────────────────────────── */

  function bind(scope, meta) {
    var q = function (id) { return scope.querySelector('#' + id); };
    if (!q('bg-card')) return;
    meta = meta || { mode: 'none' };

    var ctx = {
      meta: meta,
      bmp: null,
      prepared: null,
      previewUrl: '',
      previewOutput: meta.mode || 'none',
      previewSha: meta.sha || '',
      intensityTouched: false,
      gen: 0,
      timer: null,
      blobUrl: '',
      baseOpts: {
        output: meta.choice || 'auto',
        keyColor: meta.key || 'auto',
        tolerance: num((meta.params || {}).tolerance, 12),
        feather: num((meta.params || {}).feather, 8),
        despill: (meta.params || {}).despill !== false,
        mode: (meta.params || {}).mode || 'auto',
        invert: !!(meta.params || {}).invert,
        blur: num((meta.params || {}).blur, 2),
        maxSize: 2048,
      },
    };

    function msg(text, kind) {
      var m = q('bg-msg');
      m.textContent = text || '';
      m.className = 'small ' + (kind === 'err' ? 'text-err' : kind === 'warn' ? 'text-warn' : 'muted');
    }

    function canSave() {
      return !!(ctx.bmp || (ctx.meta.mode && ctx.meta.mode !== 'none'));
    }

    function setBadge() {
      var b = ctx.meta || {};
      var saved = b.mode && b.mode !== 'none' && b.sha;
      var label = saved
        ? ({ mask: 'Máscara', color: 'Color con alfa', original: 'Original' }[b.mode] || b.mode)
        : 'Sin fondo';
      var badge = q('bg-state');
      badge.textContent = label;
      badge.className = 'badge ' + (saved ? 'badge-ok' : 'badge-muted');
      q('bg-clear').disabled = !saved;
      q('bg-save').disabled = !canSave();
    }

    function effectiveOutput() {
      if (ctx.baseOpts.output === 'auto') {
        return ctx.prepared ? ctx.prepared.output : (ctx.meta.mode || 'auto');
      }
      return ctx.baseOpts.output;
    }

    function syncVisibility() {
      var out = effectiveOutput();
      var keyed = out === 'mask' || out === 'color';
      q('bg-tol-wrap').style.display = keyed ? '' : 'none';
      q('bg-feather-wrap').style.display = keyed ? '' : 'none';
      q('bg-mode-wrap').style.display = keyed ? '' : 'none';
      q('bg-tint').disabled = out !== 'mask';
      q('bg-tint-auto').disabled = out !== 'mask';
      q('bg-key').disabled = out === 'original';
      q('bg-key-hex').disabled = out === 'original';
      q('bg-key-auto').disabled = out === 'original';
    }

    function syncLabels() {
      q('bg-int-val').textContent = q('bg-int').value + ' %';
      q('bg-blur-val').textContent = q('bg-blur').value + ' px';
      q('bg-tol-val').textContent = q('bg-tol').value;
      q('bg-feather-val').textContent = q('bg-feather').value;
    }

    function paintPreviews() {
      var url = ctx.previewUrl;
      var out = ctx.previewOutput;
      var opacity = Math.max(0, num(q('bg-int').value, 0)) / 100;
      [['bg-prev-dark', true], ['bg-prev-light', false]].forEach(function (pair) {
        var box = q(pair[0]);
        if (!box) return;
        var layer = box.querySelector('.bg-prev-layer');
        var style = layer.style;
        style.display = 'none';
        style.backgroundImage = 'none';
        style.backgroundColor = 'transparent';
        style.webkitMaskImage = '';
        style.maskImage = '';
        if (!url || out === 'none') return;
        style.display = 'block';
        style.opacity = String(opacity);
        if (out === 'mask') {
          style.backgroundColor = pair[1] ? '#e8eef6' : '#0f172a';
          style.webkitMaskImage = 'url("' + url + '")';
          style.maskImage = 'url("' + url + '")';
          style.webkitMaskSize = 'cover';
          style.maskSize = 'cover';
          style.webkitMaskPosition = 'center';
          style.maskPosition = 'center';
        } else if (out === 'color') {
          style.backgroundImage = 'url("' + url + '")';
        } else {
          var veil = pair[1] ? 'rgba(11,15,20,.72)' : 'rgba(244,247,251,.80)';
          style.backgroundImage =
            'linear-gradient(' + veil + ',' + veil + '), url("' + url + '")';
        }
      });
    }

    function setPreview(url, output, revoke) {
      if (revoke && ctx.blobUrl) URL.revokeObjectURL(ctx.blobUrl);
      ctx.blobUrl = revoke ? url : '';
      ctx.previewUrl = url || '';
      ctx.previewOutput = output || 'none';
      paintPreviews();
    }

    function describe(prepared) {
      if (!prepared) return '—';
      var name = { mask: 'Máscara', color: 'Color con alfa', original: 'Original' }[prepared.output] || prepared.output;
      var bits = [];
      if (ctx.baseOpts.output === 'auto') bits.push('Auto → ' + name);
      else bits.push(name);
      bits.push(prepared.width + '×' + prepared.height + ' px');
      if (prepared.output !== 'original') {
        bits.push(Math.round(prepared.coverage * 100) + ' % visible');
      }
      var text = bits.join(' · ');
      if (prepared.warnings && prepared.warnings.length) text += ' — ' + prepared.warnings.join('; ');
      return text;
    }

    function syncDetect() {
      var d = q('bg-detect');
      if (ctx.prepared) {
        d.textContent = describe(ctx.prepared);
        d.className = 'hint' + (ctx.prepared.warnings.length ? ' text-warn' : '');
      } else if (ctx.meta.mode && ctx.meta.mode !== 'none') {
        d.textContent = 'Guardado: ' + (ctx.meta.mode === 'mask' ? 'máscara' : ctx.meta.mode) +
          ' · ' + num(ctx.meta.intensity, 6) + ' %';
        d.className = 'hint';
      } else {
        d.textContent = 'Elige una imagen para ver el resultado en claro y oscuro.';
        d.className = 'hint';
      }
    }

    async function regenerate() {
      if (!ctx.bmp) return;
      var my = ++ctx.gen;
      var opts = Object.assign({}, ctx.baseOpts);
      var saveBtn = q('bg-save');
      saveBtn.disabled = true;
      msg('Procesando…');
      try {
        var r = await DnxTexture.prepare(ctx.bmp, opts);
        if (my !== ctx.gen) return;
        ctx.prepared = r;
        setPreview(URL.createObjectURL(r.blob), r.output, true);
        if (!ctx.intensityTouched) {
          var rec = RECOMMENDED[r.output] != null ? RECOMMENDED[r.output] : 6;
          q('bg-int').value = String(rec);
          paintPreviews();
        }
        syncLabels();
        syncVisibility();
        syncDetect();
        setBadge();
        saveBtn.disabled = false;
        msg(r.warnings.length ? r.warnings.join('; ') : 'Listo para guardar', r.warnings.length ? 'warn' : '');
      } catch (e) {
        if (my !== ctx.gen) return;
        msg(e.message || 'No se pudo procesar la imagen', 'err');
        saveBtn.disabled = !canSave();
      }
    }

    function debounce() {
      clearTimeout(ctx.timer);
      ctx.timer = setTimeout(regenerate, 220);
    }

    async function setFile(file) {
      if (!file) return;
      if (!/^image\//.test(file.type || '')) return msg('El fichero no es una imagen', 'err');
      if (file.size > MAX_FILE) return msg('La imagen supera los 12 MB', 'err');
      msg('Leyendo imagen…');
      try {
        ctx.bmp = await DnxTexture.load(file);
        ctx.prepared = null;
        ctx.intensityTouched = false;
        var st = await DnxTexture.analyze(ctx.bmp, { maxSize: 2048 });
        ctx.baseOpts.keyColor = ctx.baseOpts.keyColor || 'auto';
        // «Auto» es la salida por defecto; el análisis solo informa.
        if (!st) throw new Error('Imagen no válida');
        syncVisibility();
        drawPicker();
        await regenerate();
      } catch (e) {
        msg(e.message || 'No se pudo leer la imagen', 'err');
      }
    }

    /* ── cuentagotas ───────────────────────────────────────────────────── */

    function drawPicker() {
      var cv = q('bg-pick-canvas');
      if (!cv || !ctx.bmp) return;
      var s = { w: ctx.bmp.width || ctx.bmp.naturalWidth, h: ctx.bmp.height || ctx.bmp.naturalHeight };
      var scale = Math.min(1, 1400 / Math.max(s.w, s.h));
      cv.width = Math.max(1, Math.round(s.w * scale));
      cv.height = Math.max(1, Math.round(s.h * scale));
      var ctx2 = cv.getContext('2d', { willReadFrequently: true });
      ctx2.drawImage(ctx.bmp, 0, 0, cv.width, cv.height);
      fitPicker();
    }

    function fitPicker() {
      var cv = q('bg-pick-canvas');
      var box = q('bg-pick-scroll');
      if (!cv || !cv.width || !box) return;
      var zoom = num(q('bg-pick-zoom').value, 1);
      var base = Math.min(1, (box.clientWidth || 480) / cv.width);
      cv.style.width = Math.round(cv.width * base * zoom) + 'px';
      cv.style.height = 'auto';
    }

    q('bg-pick-zoom').addEventListener('input', fitPicker);
    q('bg-adv').addEventListener('toggle', function () {
      if (q('bg-adv').open) { drawPicker(); }
    });
    q('bg-pick-canvas').addEventListener('click', function (ev) {
      var cv = q('bg-pick-canvas');
      var rect = cv.getBoundingClientRect();
      var x = Math.floor((ev.clientX - rect.left) / rect.width * cv.width);
      var y = Math.floor((ev.clientY - rect.top) / rect.height * cv.height);
      var d = cv.getContext('2d').getImageData(
        Math.max(0, Math.min(cv.width - 1, x)),
        Math.max(0, Math.min(cv.height - 1, y)), 1, 1
      ).data;
      var hex = '#' + [d[0], d[1], d[2]].map(function (v) {
        return v.toString(16).padStart(2, '0');
      }).join('');
      q('bg-key').value = hex;
      q('bg-key-hex').value = hex;
      ctx.baseOpts.keyColor = hex;
      debounce();
    });

    /* ── controles ─────────────────────────────────────────────────────── */

    q('bg-pick').addEventListener('click', function () { q('bg-file').click(); });
    q('bg-file').addEventListener('change', function (ev) {
      setFile(ev.target.files && ev.target.files[0]);
      ev.target.value = '';
    });

    var drop = q('bg-drop');
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.add('drag');
      });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.remove('drag');
      });
    });
    drop.addEventListener('drop', function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      setFile(f);
    });

    q('bg-out').addEventListener('change', function (ev) {
      ctx.baseOpts.output = ev.target.value;
      if (!ctx.intensityTouched) {
        var rec = RECOMMENDED[ev.target.value === 'auto'
          ? (ctx.prepared ? ctx.prepared.output : 'mask')
          : ev.target.value];
        if (rec != null) q('bg-int').value = String(rec);
      }
      syncLabels();
      syncVisibility();
      debounce();
    });

    q('bg-int').addEventListener('input', function () {
      ctx.intensityTouched = true;
      syncLabels();
      paintPreviews();
    });
    q('bg-blur').addEventListener('input', function () {
      ctx.baseOpts.blur = num(q('bg-blur').value, 2);
      syncLabels();
      debounce();
    });
    q('bg-tol').addEventListener('input', function () {
      ctx.baseOpts.tolerance = num(q('bg-tol').value, 12);
      syncLabels();
      debounce();
    });
    q('bg-feather').addEventListener('input', function () {
      ctx.baseOpts.feather = num(q('bg-feather').value, 8);
      syncLabels();
      debounce();
    });
    q('bg-despill').addEventListener('change', function (ev) {
      ctx.baseOpts.despill = ev.target.checked;
      debounce();
    });
    q('bg-invert').addEventListener('change', function (ev) {
      ctx.baseOpts.invert = ev.target.checked;
      debounce();
    });
    q('bg-mode').addEventListener('change', function (ev) {
      ctx.baseOpts.mode = ev.target.value;
      debounce();
    });
    q('bg-key').addEventListener('input', function (ev) {
      q('bg-key-hex').value = ev.target.value;
      ctx.baseOpts.keyColor = ev.target.value;
      debounce();
    });
    q('bg-key-hex').addEventListener('input', function (ev) {
      var v = String(ev.target.value || '').trim();
      if (v.toLowerCase() === 'auto') {
        ctx.baseOpts.keyColor = 'auto';
        debounce();
      } else if (/^#[0-9a-f]{6}$/i.test(v)) {
        q('bg-key').value = v;
        ctx.baseOpts.keyColor = v;
        debounce();
      }
    });
    q('bg-key-auto').addEventListener('click', function () {
      ctx.baseOpts.keyColor = 'auto';
      q('bg-key-hex').value = 'auto';
      if (ctx.prepared) q('bg-key').value = ctx.prepared.keyColor;
      debounce();
    });
    q('bg-tint-auto').addEventListener('change', function (ev) {
      q('bg-tint').disabled = ev.target.checked;
    });

    /* ── guardar / quitar ──────────────────────────────────────────────── */

    function upload(blob, params) {
      var qs = new URLSearchParams();
      Object.keys(params).forEach(function (k) {
        if (params[k] != null && params[k] !== '') qs.set(k, String(params[k]));
      });
      return fetch('/api/v1/admin/branding/background?' + qs.toString(), {
        method: 'POST',
        headers: {
          'Content-Type': blob.type || 'application/octet-stream',
          'Authorization': 'Bearer ' + S.token,
        },
        body: blob,
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (r.status === 401) {
            S.token = '';
            localStorage.removeItem('dnxt_admin');
            if (typeof showLogin === 'function') showLogin();
            throw new Error('Sesión caducada');
          }
          if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
          return d;
        });
      });
    }

    q('bg-save').addEventListener('click', async function () {
      var btn = q('bg-save');
      btn.disabled = true;
      try {
        var intensity = num(q('bg-int').value, 6);
        var tint = q('bg-tint-auto').checked ? 'auto' : q('bg-tint').value;
        if (ctx.prepared) {
          msg('Subiendo…');
          var p = ctx.prepared;
          var d = await upload(p.blob, {
            mode: p.output,
            choice: ctx.baseOpts.output,
            intensity: intensity,
            tint: tint,
            key: p.keyColor,
            coverage: p.coverage,
            params: JSON.stringify(p.params),
          });
          ctx.meta = d.background || {};
          ctx.previewUrl = bgUrl(ctx.meta.sha);
          ctx.previewOutput = ctx.meta.mode;
          ctx.previewSha = ctx.meta.sha;
          paintPreviews();
          apply(ctx.meta);
          setBadge();
          syncDetect();
          msg('Fondo guardado', 'ok');
        } else if (ctx.meta.mode && ctx.meta.mode !== 'none') {
          msg('Guardando ajustes…');
          await api('/admin/settings', {
            method: 'PATCH',
            body: JSON.stringify({ bg_intensity: intensity, bg_tint: tint }),
          });
          ctx.meta.intensity = intensity;
          ctx.meta.tint = tint;
          apply(ctx.meta);
          setBadge();
          msg('Ajustes guardados', 'ok');
        } else {
          msg('Elige una imagen primero', 'warn');
        }
      } catch (e) {
        msg(e.message || 'No se pudo guardar', 'err');
      } finally {
        btn.disabled = !canSave();
      }
    });

    q('bg-clear').addEventListener('click', async function () {
      if (!confirm('¿Quitar el fondo de la aplicación?')) return;
      try {
        await api('/admin/branding/background', { method: 'DELETE' });
        ctx.meta = { mode: 'none' };
        ctx.prepared = null;
        setPreview('', 'none', true);
        apply(null);
        setBadge();
        syncVisibility();
        syncDetect();
        msg('Fondo quitado', 'ok');
      } catch (e) {
        msg(e.message || 'No se pudo quitar', 'err');
      }
    });

    /* ── estado inicial ────────────────────────────────────────────────── */

    if (meta.mode && meta.mode !== 'none' && meta.sha) {
      setPreview(bgUrl(meta.sha), meta.mode, false);
    } else {
      setPreview('', 'none', false);
    }
    q('bg-out').value = ctx.baseOpts.output || 'auto';
    q('bg-mode').value = ctx.baseOpts.mode;
    q('bg-key-hex').value = ctx.baseOpts.keyColor || 'auto';
    syncLabels();
    syncVisibility();
    syncDetect();
    setBadge();
  }

  root.DnxBg = {
    apply: apply,
    load: load,
    card: card,
    bind: bind,
    state: function () { return STATE; },
  };
})(typeof window !== 'undefined' ? window : globalThis);
