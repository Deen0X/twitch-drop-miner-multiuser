/* DNX Background v1 — preparación de texturas de fondo (canvas, sin dependencias).
 *
 * Todo el trabajo pesado ocurre UNA vez, al elegir la imagen. El resultado se
 * sube al servidor y se sirve tal cual; en runtime solo hay CSS.
 *
 *   const r = await DnxTexture.prepare(file, {
 *     output: 'auto',        // 'auto' | 'mask' | 'color' | 'original'
 *     keyColor: 'auto',      // '#rrggbb' | 'auto' (color del borde)
 *     tolerance: 12,         // 0-100  (solo chroma)
 *     feather: 8,            // 0-100  (solo chroma)
 *     despill: true,         // quita el halo del color eliminado
 *     mode: 'auto',          // 'auto' | 'chroma' | 'luma'
 *     invert: false,         // invierte el alfa
 *     blur: 2,               // px de suavizado
 *     maxSize: 2048,         // lado máximo de salida
 *   });
 *   // r = { blob, mime, ext, output, mode, keyColor, coverage, stats, warnings,
 *   //       width, height, params }
 *
 * Salidas:
 *   mask     -> blanco + alfa de la textura. Se pinta con mask-image y el color
 *               del tema (funciona en claro/oscuro con un solo fichero).
 *   color    -> colores originales con el color clave hecho transparente.
 *   original -> la imagen tal cual (reescalada). El velo del tema lo pone el CSS.
 */
(function (root) {
  'use strict';

  var DEFAULTS = {
    output: 'auto',
    keyColor: 'auto',
    tolerance: 12,
    feather: 8,
    despill: true,
    mode: 'auto',
    invert: false,
    blur: 2,
    maxSize: 2048,
    quality: 0.92,
  };

  /* ── utilidades ──────────────────────────────────────────────────────── */

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  function smoothstep(a, b, x) {
    if (b <= a) return x < a ? 0 : 1;
    var t = clamp((x - a) / (b - a), 0, 1);
    return t * t * (3 - 2 * t);
  }

  function normHex(v) {
    var s = String(v || '').trim();
    if (/^#[0-9a-f]{3}$/i.test(s)) {
      s = '#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
    }
    if (!/^#[0-9a-f]{6}$/i.test(s)) return null;
    return s.toLowerCase();
  }

  function hexToRgb(hex) {
    var h = normHex(hex) || '#ffffff';
    return {
      r: parseInt(h.slice(1, 3), 16),
      g: parseInt(h.slice(3, 5), 16),
      b: parseInt(h.slice(5, 7), 16),
    };
  }

  function rgbToHex(r, g, b) {
    var f = function (v) {
      return clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0');
    };
    return '#' + f(r) + f(g) + f(b);
  }

  function makeCanvas(w, h) {
    var c = document.createElement('canvas');
    c.width = Math.max(1, w | 0);
    c.height = Math.max(1, h | 0);
    return c;
  }

  function sizeOf(bmp) {
    return {
      w: bmp.width || bmp.naturalWidth || 0,
      h: bmp.height || bmp.naturalHeight || 0,
    };
  }

  async function loadBitmap(src) {
    if (!src) throw new Error('Falta la imagen');
    if (typeof src === 'string') {
      var img = new Image();
      img.crossOrigin = 'anonymous';
      img.src = src;
      if (img.decode) await img.decode();
      return img;
    }
    var isBlob = (typeof Blob !== 'undefined' && src instanceof Blob);
    if (isBlob) {
      if (typeof createImageBitmap === 'function') {
        try {
          return await createImageBitmap(src, { imageOrientation: 'from-image' });
        } catch (e) { /* fallback a <img> */ }
      }
      var url = URL.createObjectURL(src);
      try {
        var im = new Image();
        im.src = url;
        if (im.decode) await im.decode();
        else await new Promise(function (ok, ko) { im.onload = ok; im.onerror = ko; });
        return im;
      } finally {
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      }
    }
    return src; // ImageBitmap / HTMLImageElement / HTMLCanvasElement
  }

  /* Dibuja la imagen en un canvas de trabajo, reescalando a maxSize. */
  function drawWork(bmp, maxSize) {
    var s = sizeOf(bmp);
    if (!s.w || !s.h) throw new Error('Imagen sin tamaño');
    var scale = Math.min(1, (maxSize || 2048) / Math.max(s.w, s.h));
    var W = Math.max(1, Math.round(s.w * scale));
    var H = Math.max(1, Math.round(s.h * scale));
    var c = makeCanvas(W, H);
    var ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bmp, 0, 0, W, H);
    return {
      canvas: c, ctx: ctx, data: ctx.getImageData(0, 0, W, H),
      width: W, height: H,
    };
  }

  /* ── desenfoque (sumas acumuladas, bordes replicados) ────────────────── */

  /* Desenfoca un canal de 8 bits. `arr` es RGBA; `offset` 0/1/2/3. */
  function blurChannel(arr, w, h, radius, offset) {
    var r = Math.round(radius);
    if (r < 1) return;
    var src = new Float32Array(w * h);
    var n = w * h, i;
    for (i = 0; i < n; i++) src[i] = arr[i * 4 + offset];

    // Dos pasadas de caja ≈ triángulo (más suave que una sola).
    for (var pass = 0; pass < 2; pass++) {
      src = boxPass(src, w, h, r, true);
      src = boxPass(src, w, h, r, false);
    }
    for (i = 0; i < n; i++) arr[i * 4 + offset] = clamp(src[i], 0, 255);
  }

  function boxPass(src, w, h, r, horizontal) {
    var out = new Float32Array(w * h);
    var len = horizontal ? w : h;
    var lines = horizontal ? h : w;
    var win = 2 * r + 1;
    var sum, k, idx, pos, line, base;
    for (line = 0; line < lines; line++) {
      base = horizontal ? line * w : line;
      // suma inicial (bordes replicados)
      sum = 0;
      for (k = -r; k <= r; k++) {
        pos = clamp(k, 0, len - 1);
        sum += src[horizontal ? base + pos : pos * w + line];
      }
      for (idx = 0; idx < len; idx++) {
        out[horizontal ? base + idx : idx * w + line] = sum / win;
        var outPos = clamp(idx - r, 0, len - 1);
        var inPos = clamp(idx + r + 1, 0, len - 1);
        sum -= src[horizontal ? base + outPos : outPos * w + line];
        sum += src[horizontal ? base + inPos : inPos * w + line];
      }
    }
    return out;
  }

  /* ── análisis ────────────────────────────────────────────────────────── */

  function analyzePixels(d) {
    var px = d.data, w = d.width, h = d.height;
    var n = 0, chromaSum = 0, satSum = 0;
    var step = Math.max(1, Math.floor(Math.sqrt((w * h) / 250000)));
    var x, y;
    for (y = 0; y < h; y += step) {
      for (x = 0; x < w; x += step) {
        var i = (y * w + x) * 4;
        var r = px[i], g = px[i + 1], b = px[i + 2];
        var mx = Math.max(r, g, b), mn = Math.min(r, g, b);
        chromaSum += mx - mn;
        satSum += mx ? (mx - mn) / mx : 0;
        n++;
      }
    }
    var meanChroma = n ? chromaSum / n : 0;
    var meanSat = n ? satSum / n : 0;

    // Color del borde (banda del 2 %, mínimo 2 px) cuantizado a 4 bits/canal.
    var band = Math.max(2, Math.round(Math.min(w, h) * 0.02));
    var counts = new Map();
    var samples = 0;
    for (y = 0; y < h; y++) {
      var edgeY = (y < band || y >= h - band);
      for (x = 0; x < w; x++) {
        if (!edgeY && x >= band && x < w - band) continue;
        var j = (y * w + x) * 4;
        var key = ((px[j] >> 4) << 8) | ((px[j + 1] >> 4) << 4) | (px[j + 2] >> 4);
        counts.set(key, (counts.get(key) || 0) + 1);
        samples++;
      }
    }
    var bestKey = 0, bestN = 0;
    counts.forEach(function (v, k) { if (v > bestN) { bestN = v; bestKey = k; } });
    var uniformity = samples ? bestN / samples : 0;

    // Cobertura de ese color en toda la imagen (muestreada).
    var hits = 0, total = 0;
    for (y = 0; y < h; y += step) {
      for (x = 0; x < w; x += step) {
        var q = (y * w + x) * 4;
        var k2 = ((px[q] >> 4) << 8) | ((px[q + 1] >> 4) << 4) | (px[q + 2] >> 4);
        if (k2 === bestKey) hits++;
        total++;
      }
    }
    var coverage = total ? hits / total : 0;

    var keyColor = rgbToHex(
      ((bestKey >> 8) & 15) * 17, ((bestKey >> 4) & 15) * 17, (bestKey & 15) * 17
    );
    return {
      width: w, height: h,
      meanChroma: Math.round(meanChroma * 10) / 10,
      meanSat: Math.round(meanSat * 1000) / 1000,
      gray: meanChroma < 10,
      borderColor: keyColor,
      borderUniformity: Math.round(uniformity * 1000) / 1000,
      borderCoverage: Math.round(coverage * 1000) / 1000,
      flat: uniformity > 0.6 && coverage > 0.35,
    };
  }

  function autoOutput(stats) {
    if (stats.gray) return 'mask';
    if (stats.flat) return 'color';
    return 'original';
  }

  function autoMode(output, stats) {
    if (output === 'mask') return stats.flat ? 'chroma' : 'luma';
    return 'chroma';
  }

  /* ── keying ──────────────────────────────────────────────────────────── */

  /* Devuelve la cobertura (fracción de píxeles visibles). */
  function keyPixels(img, o, output, mode, keyRgb) {
    var px = img.data, len = px.length;
    var t0 = o.tolerance * 1.5;
    var t1 = t0 + o.feather * 2 + 1;
    var k = keyRgb;
    var visible = 0, count = 0;

    for (var i = 0; i < len; i += 4) {
      var r = px[i], g = px[i + 1], b = px[i + 2];
      var a;
      if (output === 'original') {
        a = 1;
      } else if (mode === 'luma') {
        a = 1 - (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      } else {
        var d = Math.max(Math.abs(r - k.r), Math.abs(g - k.g), Math.abs(b - k.b));
        a = smoothstep(t0, t1, d);
      }
      if (o.invert && output !== 'original') a = 1 - a;

      if (output === 'mask') {
        px[i] = 255; px[i + 1] = 255; px[i + 2] = 255;
      } else if (mode === 'chroma' && o.despill && a > 0.02 && a < 0.98) {
        // Des-mezcla con el color clave para quitar el halo del borde.
        px[i] = (r - (1 - a) * k.r) / a;
        px[i + 1] = (g - (1 - a) * k.g) / a;
        px[i + 2] = (b - (1 - a) * k.b) / a;
      }
      px[i + 3] = Math.round(clamp(a, 0, 1) * 255);
      if (a > 0.5) visible++;
      count++;
    }
    return count ? visible / count : 0;
  }

  /* Rellena el RGB de los píxeles transparentes con el de sus vecinos.
   * Evita flecos al escalar (el navegador interpola RGB aunque el alfa sea 0). */
  function alphaBleed(px, w, h) {
    var offs = [[-1, 0], [1, 0], [0, -1], [0, 1]];
    for (var pass = 0; pass < 2; pass++) {
      var snapshot = new Uint8ClampedArray(px);
      for (var y = 1; y < h - 1; y++) {
        for (var x = 1; x < w - 1; x++) {
          var i = (y * w + x) * 4;
          if (snapshot[i + 3] > 8) continue;
          var r = 0, g = 0, b = 0, c = 0;
          for (var k = 0; k < 4; k++) {
            var j = ((y + offs[k][1]) * w + (x + offs[k][0])) * 4;
            if (snapshot[j + 3] > 8) {
              r += snapshot[j]; g += snapshot[j + 1]; b += snapshot[j + 2]; c++;
            }
          }
          if (c) { px[i] = r / c; px[i + 1] = g / c; px[i + 2] = b / c; }
        }
      }
    }
  }

  function encode(canvas, quality) {
    var supportsWebp = false;
    try {
      supportsWebp = canvas.toDataURL('image/webp').indexOf('data:image/webp') === 0;
    } catch (e) { supportsWebp = false; }
    var mime = supportsWebp ? 'image/webp' : 'image/png';
    return new Promise(function (ok, ko) {
      canvas.toBlob(function (blob) {
        if (!blob) return ko(new Error('No se pudo codificar la imagen'));
        var real = blob.type || mime;
        ok({
          blob: blob,
          mime: real,
          ext: real === 'image/png' ? 'png' : (real === 'image/jpeg' ? 'jpg' : 'webp'),
        });
      }, mime, quality);
    });
  }

  /* ── API ─────────────────────────────────────────────────────────────── */

  async function analyze(source, options) {
    var o = Object.assign({}, DEFAULTS, options || {});
    var bmp = await loadBitmap(source);
    var work = drawWork(bmp, Math.min(o.maxSize || 2048, 768));
    return analyzePixels(work.data);
  }

  function optionsFor(o, stats) {
    var output = o.output === 'auto' ? autoOutput(stats) : o.output;
    if (['mask', 'color', 'original'].indexOf(output) < 0) output = 'original';
    var mode = o.mode === 'auto' ? autoMode(output, stats) : o.mode;
    if (['chroma', 'luma'].indexOf(mode) < 0) mode = 'chroma';
    var keyHex = normHex(o.keyColor) || stats.borderColor;
    return { output: output, mode: mode, keyHex: keyHex, keyRgb: hexToRgb(keyHex) };
  }

  async function prepare(source, options) {
    var o = Object.assign({}, DEFAULTS, options || {});
    var warnings = [];
    var bmp = await loadBitmap(source);
    var work = drawWork(bmp, o.maxSize || 2048);
    var img = work.data;

    var stats = analyzePixels(img);
    var sel = optionsFor(o, stats);

    var coverage = keyPixels(img, o, sel.output, sel.mode, sel.keyRgb);

    if (o.blur > 0) {
      if (sel.output === 'original') {
        blurChannel(img.data, work.width, work.height, o.blur, 0);
        blurChannel(img.data, work.width, work.height, o.blur, 1);
        blurChannel(img.data, work.width, work.height, o.blur, 2);
      } else {
        // Suaviza solo el alfa: conserva los colores y las zonas opacas.
        blurChannel(img.data, work.width, work.height, o.blur, 3);
        if (sel.output === 'color') alphaBleed(img.data, work.width, work.height);
      }
    }

    if (sel.output !== 'original') {
      if (coverage < 0.03) warnings.push('Casi toda la imagen queda transparente');
      if (coverage > 0.97) warnings.push('Casi toda la imagen queda opaca');
      if (sel.output === 'mask' && !stats.gray && !stats.flat) {
        warnings.push('La imagen tiene color: quizá prefieras «Color con alfa» u «Original»');
      }
      if (sel.output === 'color' && !stats.flat) {
        warnings.push('No se detecta un fondo plano: el recorte puede quedar irregular');
      }
    }

    work.ctx.putImageData(img, 0, 0);
    var enc = await encode(work.canvas, o.quality);

    return {
      blob: enc.blob,
      mime: enc.mime,
      ext: enc.ext,
      output: sel.output,
      mode: sel.mode,
      keyColor: sel.keyHex,
      coverage: Math.round(coverage * 1000) / 1000,
      stats: stats,
      warnings: warnings,
      width: work.width,
      height: work.height,
      params: {
        tolerance: o.tolerance, feather: o.feather, despill: !!o.despill,
        invert: !!o.invert, blur: o.blur, maxSize: o.maxSize,
      },
    };
  }

  root.DnxTexture = {
    prepare: prepare,
    analyze: analyze,
    load: loadBitmap,
    DEFAULTS: DEFAULTS,
    _internals: {
      analyzePixels: analyzePixels,
      autoOutput: autoOutput,
      autoMode: autoMode,
      hexToRgb: hexToRgb,
      normHex: normHex,
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
