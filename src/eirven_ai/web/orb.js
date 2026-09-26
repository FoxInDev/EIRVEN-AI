(() => {
  'use strict';

  /* Сфера Эрви — процедурная отрисовка.
   *
   * Раньше материал собирался из полупрозрачных CSS-слоёв поверх PNG. Так нельзя
   * получить жидкость: слой стоит по прямоугольнику, а сфера в картинке смещена и
   * не занимает весь кадр, поэтому края не совпадали и всё читалось как наклейка.
   * Здесь сфера считается каждый кадр по своим координатам, поэтому она не может
   * разъехаться с собственной кромкой.
   *
   * Canvas 2D, а не WebGL: на слабых видеокартах и в виртуальных машинах контекст
   * WebGL теряется или не создаётся вовсе, а сфера — главный элемент интерфейса,
   * который обязан рисоваться всегда.
   */

  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function boot(host, opts) {
    const canvas = host.querySelector('canvas.orb-canvas');
    if (!canvas) return null;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;                 // без контекста остаётся исходная картинка

    const img = host.querySelector('img');
    if (img) img.style.visibility = 'hidden';   // PNG больше не участвует в отрисовке

    let W = 0, H = 0, R = 0, cx = 0, cy = 0, dpr = 1;
    const ripples = [];
    let phase = Math.random() * 1000;
    let energy = 0;              // 0..1 — оживает во время речи и нажатий
    let running = false, raf = 0;

    function resize() {
      const rect = host.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      dpr = Math.min(window.devicePixelRatio || 1, 2);   // выше 2 разницы не видно, а нагрузка растёт вчетверо
      W = Math.round(rect.width);
      H = Math.round(rect.height);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = W + 'px';
      canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx = W / 2; cy = H / 2;
      R = Math.min(W, H) * 0.46;
      return true;
    }

    /* Кромка не идеально круглая: поверхность под натяжением всегда чуть
       колышется, и именно это отличает жидкость от пластикового шара. */
    function edge(a, t) {
      return R * (1
        + 0.022 * Math.sin(a * 3 + t * 0.7)
        + 0.014 * Math.sin(a * 5 - t * 0.9)
        + 0.010 * Math.sin(a * 2 + t * 1.3)
        + rippleOffset(a, t));
    }

    function rippleOffset(a, t) {
      let sum = 0;
      for (const r of ripples) {
        const age = t - r.t0;
        if (age < 0 || age > r.life) continue;
        // Волна расходится от точки удара и затухает к краям.
        const da = Math.abs(((a - r.angle + Math.PI * 3) % (Math.PI * 2)) - Math.PI);
        const front = age / r.life;
        const ring = Math.exp(-Math.pow((da / Math.PI - front) * 3.2, 2));
        sum += r.power * ring * (1 - front) * Math.sin(age * 14);
      }
      return sum;
    }

    function spherePath(t) {
      ctx.beginPath();
      const steps = 96;
      for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * Math.PI * 2;
        const rr = edge(a, t);
        const x = cx + Math.cos(a) * rr;
        const y = cy + Math.sin(a) * rr;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
    }

    /* Полосы преломления в толще. Рисуются дугами разной ширины и цвета,
       сложение через 'screen' оставляет ядро тёмным и светит только полосами. */
    function drawBands(t) {
      const bands = [
        { off: 0.00, w: 0.30, c: 'rgba(150,110,255,',  a: 0.42, sp:  0.18 },
        { off: 1.90, w: 0.22, c: 'rgba(96,196,255,',   a: 0.34, sp: -0.13 },
        { off: 3.40, w: 0.16, c: 'rgba(255,170,90,',   a: 0.20, sp:  0.09 },
        { off: 4.60, w: 0.26, c: 'rgba(190,120,255,',  a: 0.36, sp: -0.20 },
        { off: 5.60, w: 0.18, c: 'rgba(90,210,255,',   a: 0.26, sp:  0.15 }
      ];
      ctx.globalCompositeOperation = 'screen';
      for (const b of bands) {
        const a0 = b.off + t * b.sp;
        const grad = ctx.createLinearGradient(
          cx + Math.cos(a0) * R, cy + Math.sin(a0) * R,
          cx - Math.cos(a0) * R, cy - Math.sin(a0) * R
        );
        grad.addColorStop(0,   b.c + '0)');
        grad.addColorStop(0.4, b.c + (b.a * (0.75 + energy * 0.5)).toFixed(3) + ')');
        grad.addColorStop(0.55, b.c + (b.a * 0.5).toFixed(3) + ')');
        grad.addColorStop(1,   b.c + '0)');
        ctx.save();
        ctx.translate(cx, cy);
        // Полосы идут по дуге, а не прямо: так они «облегают» объём.
        ctx.rotate(Math.sin(t * 0.23 + b.off) * 0.30);
        ctx.scale(1, 0.62 + 0.16 * Math.sin(t * 0.31 + b.off));
        ctx.translate(-cx, -cy);
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.ellipse(cx, cy, R * (0.86 + b.w), R * (0.86 + b.w), 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
      ctx.globalCompositeOperation = 'source-over';
    }

    function draw(now) {
      raf = 0;
      if (!running) return;
      const t = REDUCED ? 12.0 : (now / 1000) + phase;

      // Затухание отклика на касание и на речь.
      energy *= 0.965;
      for (let i = ripples.length - 1; i >= 0; i--) {
        if (t - ripples[i].t0 > ripples[i].life) ripples.splice(i, 1);
      }

      ctx.clearRect(0, 0, W, H);

      // Свечение вокруг: сфера должна светить в фон, иначе выглядит вырезанной.
      const halo = ctx.createRadialGradient(cx, cy, R * 0.7, cx, cy, R * 1.5);
      halo.addColorStop(0, `rgba(120,80,230,${(0.20 + energy * 0.18).toFixed(3)})`);
      halo.addColorStop(0.55, 'rgba(90,70,200,.07)');
      halo.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, W, H);

      ctx.save();
      spherePath(t);
      ctx.clip();               // всё дальнейшее живёт строго внутри кромки

      // Тёмное ядро.
      const core = ctx.createRadialGradient(cx, cy - R * 0.12, R * 0.05, cx, cy, R);
      core.addColorStop(0,   'rgba(10,10,30,.97)');
      core.addColorStop(0.55,'rgba(8,9,26,.93)');
      core.addColorStop(0.86,'rgba(24,18,58,.86)');
      core.addColorStop(1,   'rgba(60,40,120,.55)');
      ctx.fillStyle = core;
      ctx.fillRect(0, 0, W, H);

      drawBands(t);

      // Внутреннее свечение у кромки: свет собирается по краю, как в капле.
      ctx.globalCompositeOperation = 'screen';
      const inner = ctx.createRadialGradient(cx, cy, R * 0.62, cx, cy, R * 1.02);
      inner.addColorStop(0, 'rgba(0,0,0,0)');
      inner.addColorStop(0.82, `rgba(150,120,255,${(0.20 + energy * 0.2).toFixed(3)})`);
      inner.addColorStop(1, `rgba(200,220,255,${(0.42 + energy * 0.3).toFixed(3)})`);
      ctx.fillStyle = inner;
      ctx.fillRect(0, 0, W, H);

      // Блик сверху и мягкий отсвет снизу.
      const gloss = ctx.createRadialGradient(
        cx - R * 0.30, cy - R * 0.40, R * 0.02,
        cx - R * 0.22, cy - R * 0.34, R * 0.66
      );
      gloss.addColorStop(0, 'rgba(255,255,255,.42)');
      gloss.addColorStop(0.45, 'rgba(210,230,255,.10)');
      gloss.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = gloss;
      ctx.fillRect(0, 0, W, H);

      const bounce = ctx.createRadialGradient(
        cx + R * 0.18, cy + R * 0.52, R * 0.02,
        cx + R * 0.18, cy + R * 0.52, R * 0.5
      );
      bounce.addColorStop(0, 'rgba(120,200,255,.24)');
      bounce.addColorStop(1, 'rgba(120,200,255,0)');
      ctx.fillStyle = bounce;
      ctx.fillRect(0, 0, W, H);
      ctx.globalCompositeOperation = 'source-over';
      ctx.restore();

      // Кромка поверх заливки — тонкая яркая линия по самому краю.
      ctx.save();
      spherePath(t);
      ctx.lineWidth = Math.max(1, R * 0.014);
      const rim = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
      rim.addColorStop(0,   `rgba(190,150,255,${(0.75 + energy * 0.25).toFixed(3)})`);
      rim.addColorStop(0.45,`rgba(230,240,255,${(0.85 + energy * 0.15).toFixed(3)})`);
      rim.addColorStop(1,   `rgba(110,190,255,${(0.70 + energy * 0.3).toFixed(3)})`);
      ctx.strokeStyle = rim;
      ctx.shadowColor = 'rgba(150,120,255,.75)';
      ctx.shadowBlur = R * 0.16;
      ctx.stroke();
      ctx.restore();

      raf = window.requestAnimationFrame(draw);
    }

    function start() {
      if (running) return;
      if (!resize()) return;
      running = true;
      if (!raf) raf = window.requestAnimationFrame(draw);
    }
    function stop() {
      running = false;
      if (raf) { window.cancelAnimationFrame(raf); raf = 0; }
    }

    // Не тратим кадры, когда сферы не видно: на вкладке в фоне и за экраном.
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((entries) => {
        entries.forEach(e => e.isIntersecting ? start() : stop());
      }, { threshold: 0.05 }).observe(host);
    } else {
      start();
    }
    document.addEventListener('visibilitychange', () => {
      document.hidden ? stop() : start();
    });

    let resizeTimer = 0;
    window.addEventListener('resize', () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => { resize(); }, 120);
    }, { passive: true });

    if (opts && opts.interactive !== false) {
      host.addEventListener('pointerdown', (event) => {
        const rect = host.getBoundingClientRect();
        if (!rect.width) return;
        const dx = (event.clientX - rect.left) - cx;
        const dy = (event.clientY - rect.top) - cy;
        ripples.push({
          t0: (performance.now() / 1000) + phase,
          angle: Math.atan2(dy, dx),
          power: 0.055,
          life: 1.15
        });
        energy = Math.min(1, energy + 0.55);
        if (!running) start();
      });
    }

    return {
      // Оживление во время речи — вызывается из общего кода интерфейса.
      pulse(level) { energy = Math.min(1, Math.max(energy, Number(level) || 0)); },
      resize, start, stop
    };
  }

  function init() {
    const hosts = document.querySelectorAll('[data-orb-canvas]');
    const made = [];
    hosts.forEach(h => { const inst = boot(h, { interactive: true }); if (inst) made.push(inst); });
    if (made.length) window.eirvenOrb = made[0];
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
