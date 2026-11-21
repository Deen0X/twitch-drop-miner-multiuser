/* DNX Web UI — tema cíclico, drawer, modales, toast y refresh (vanilla). */
(function () {
  'use strict';

  /* ── Tema Auto / Oscuro / Claro ─────────────────────────────── */
  var THEME_KEY = 'dnx_theme';
  var SVG_OPEN = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">';
  var ICONS = {
    auto: SVG_OPEN + '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg>',
    dark: SVG_OPEN + '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
    light: SVG_OPEN + '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>'
  };
  var LABELS = { auto: 'Auto', dark: 'Dark', light: 'Light' };
  var NEXT = { auto: 'dark', dark: 'light', light: 'auto' };
  var theme = localStorage.getItem(THEME_KEY) || 'auto';

  function applyTheme(t) {
    t = NEXT[t] ? t : 'auto';
    theme = t;
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    var ico = document.getElementById('theme-ico');
    var lbl = document.getElementById('theme-lbl');
    var btn = document.getElementById('theme-btn');
    if (btn) btn.setAttribute('aria-pressed', t === 'auto' ? 'false' : 'mixed');
    if (ico) ico.innerHTML = ICONS[t];
    if (lbl) lbl.textContent = LABELS[t];
  }

  /* ── Toast ──────────────────────────────────────────────────── */
  function toast(message, kind) {
    var box = document.getElementById('toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast';
      document.body.appendChild(box);
    }
    var el = document.createElement('div');
    if (kind) el.className = kind;
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      el.style.transition = 'opacity .2s, transform .2s';
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, 3200);
  }

  /* ── Modales ────────────────────────────────────────────────── */
  function openModal(id) {
    var m = document.getElementById(id);
    if (m) m.classList.add('open');
    return m;
  }
  function closeModal(id) {
    var m = document.getElementById(id);
    if (m) m.classList.remove('open');
    return m;
  }
  function closeAllModals() {
    document.querySelectorAll('.modal.open').forEach(function (m) { m.classList.remove('open'); });
  }

  /* ── Drawer móvil ───────────────────────────────────────────── */
  function sidebar() { return document.querySelector('.sidebar'); }
  function overlay() { return document.getElementById('drawer-overlay'); }
  function closeDrawer() {
    var sb = sidebar(), ov = overlay();
    if (sb) sb.classList.remove('open');
    if (ov) ov.classList.remove('open');
  }
  function toggleDrawer() {
    var sb = sidebar(), ov = overlay();
    if (!sb) return;
    var open = sb.classList.toggle('open');
    if (ov) ov.classList.toggle('open', open);
  }

  /* ── Visor de imágenes (zoom + pan) ─────────────────────────── */
  var _viewer = null;
  var _vz = { s: 1, x: 0, y: 0, drag: false, px: 0, py: 0 };

  function _applyVz() {
    if (!_viewer) return;
    var img = _viewer.querySelector('.dnx-img-viewer-img');
    if (!img) return;
    img.style.transform = 'translate(' + _vz.x + 'px,' + _vz.y + 'px) scale(' + _vz.s + ')';
    img.style.cursor = _vz.s > 1 ? 'grab' : 'zoom-in';
  }

  function _resetVz() {
    _vz = { s: 1, x: 0, y: 0, drag: false, px: 0, py: 0 };
    _applyVz();
  }

  function _ensureViewer() {
    if (_viewer) return _viewer;
    var wrap = document.createElement('div');
    wrap.id = 'dnx-img-viewer';
    wrap.className = 'dnx-img-viewer';
    wrap.innerHTML =
      '<img class="dnx-img-viewer-img" alt="">' +
      '<div class="dnx-img-viewer-cap">' +
      '<div class="n"></div><div class="s"></div>' +
      '<div class="h">Scroll to zoom · drag to pan · double-click to reset</div>' +
      '</div>' +
      '<button class="btn dnx-img-viewer-close" type="button">Close</button>';
    document.body.appendChild(wrap);

    var img = wrap.querySelector('.dnx-img-viewer-img');
    wrap.addEventListener('click', function (e) {
      if (e.target === wrap) closeImage();
    });
    wrap.querySelector('.dnx-img-viewer-close').addEventListener('click', closeImage);

    img.addEventListener('dblclick', function (e) { e.stopPropagation(); _resetVz(); });
    img.addEventListener('mousedown', function (e) {
      if (_vz.s <= 1) return;
      _vz.drag = true;
      _vz.px = e.clientX - _vz.x;
      _vz.py = e.clientY - _vz.y;
      img.style.cursor = 'grabbing';
      e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!_vz.drag) return;
      _vz.x = e.clientX - _vz.px;
      _vz.y = e.clientY - _vz.py;
      _applyVz();
    });
    window.addEventListener('mouseup', function () {
      if (!_vz.drag) return;
      _vz.drag = false;
      _applyVz();
    });
    window.addEventListener('wheel', function (e) {
      if (!_viewer || !_viewer.classList.contains('open')) return;
      e.preventDefault();
      _vz.s = Math.min(6, Math.max(1, _vz.s + (e.deltaY < 0 ? 0.2 : -0.2)));
      if (_vz.s === 1) { _vz.x = 0; _vz.y = 0; }
      _applyVz();
    }, { passive: false });

    _viewer = wrap;
    return wrap;
  }

  function openImage(src, name, sub) {
    var wrap = _ensureViewer();
    wrap.querySelector('.dnx-img-viewer-img').src = src || '';
    wrap.querySelector('.dnx-img-viewer-cap .n').textContent = name || '';
    wrap.querySelector('.dnx-img-viewer-cap .s').textContent = sub || '';
    _resetVz();
    wrap.classList.add('open');
  }

  function closeImage() {
    if (_viewer) _viewer.classList.remove('open');
    _resetVz();
  }

  /* ── API helper para scripts de página ────────────────────────── */

  var S = { token: '' };
  function api(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (S.token) opts.headers['Authorization'] = 'Bearer ' + S.token;
    return fetch(url, opts).then(function (r) {
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

  /* ── API pública ────────────────────────────────────────────── */
  window.DNXWebUI = {
    applyTheme: applyTheme,
    getTheme: function () { return theme; },
    toast: toast,
    openModal: openModal,
    closeModal: closeModal,
    closeAllModals: closeAllModals,
    toggleDrawer: toggleDrawer,
    closeDrawer: closeDrawer,
    openImage: openImage,
    closeImage: closeImage
  };

  applyTheme(theme);

  /* ── Delegación de eventos ──────────────────────────────────── */
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;

    if (t.closest('#theme-btn')) { applyTheme(NEXT[theme]); return; }
    if (t.closest('#menu-btn')) { toggleDrawer(); return; }
    if (t.closest('#drawer-overlay')) { closeDrawer(); return; }

    // Cerrar drawer al elegir navegación
    if (t.closest('.nav a, .drawer-nav a')) closeDrawer();

    // Cerrar modal con [data-close] o clic en el overlay
    var closer = t.closest('[data-close]');
    if (closer) {
      var m1 = closer.closest('.modal');
      if (m1) m1.classList.remove('open');
      return;
    }
    if (t.classList && t.classList.contains('modal')) {
      t.classList.remove('open');
      return;
    }

    // Clic fuera del sidebar con drawer abierto
    var sb = sidebar();
    if (sb && sb.classList.contains('open') && !t.closest('.sidebar')) closeDrawer();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeDrawer(); closeAllModals(); closeImage(); }
  });

  // LED de estado = refrescar
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('#live')) {
      document.dispatchEvent(new CustomEvent('dnx:refresh'));
    }
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && document.activeElement &&
        document.activeElement.id === 'live') {
      e.preventDefault();
      document.dispatchEvent(new CustomEvent('dnx:refresh'));
    }
  });
})();
