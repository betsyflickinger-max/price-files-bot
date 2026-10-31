/* area-menu.js: The Healthcare Files. Written to /area-menu.js on every deploy by site/area_menu.py, which fills in AREA_MAP.
   Oct 31, 2026 (Betsy): every area dropdown, on the price site and the control center, lists only states. A state with more
   than one place (Texas metros, California's regions) opens its places beside it on hover, or on click or tap. A state with
   one place picks it straight away.
   The page's own <select> stays in the page, hidden, and stays the source of truth: picking an item sets its value and fires
   "change", and code that sets .value or refills the options updates the button. So no page code had to change. */
(function () {
  'use strict';
  if (window.__areaMenu) return; window.__areaMenu = 1;
  var MAP = /*AREA_MAP*/{names: {}, keys: {}}/*AREA_MAP_END*/;
  var SEL = '#natArea, select#area, select[name="area"], #market, #nmarket, #c-mkt, #o-mkt, #o-parea';
  var SKIP = '#f-area, [data-area-menu="off"]';
  var US = ['Alabama', 'Alaska', 'Arizona', 'Arkansas', 'California', 'Colorado', 'Connecticut', 'Delaware', 'District of Columbia',
    'Florida', 'Georgia', 'Hawaii', 'Idaho', 'Illinois', 'Indiana', 'Iowa', 'Kansas', 'Kentucky', 'Louisiana', 'Maine', 'Maryland',
    'Massachusetts', 'Michigan', 'Minnesota', 'Mississippi', 'Missouri', 'Montana', 'Nebraska', 'Nevada', 'New Hampshire', 'New Jersey',
    'New Mexico', 'New York', 'North Carolina', 'North Dakota', 'Ohio', 'Oklahoma', 'Oregon', 'Pennsylvania', 'Rhode Island',
    'South Carolina', 'South Dakota', 'Tennessee', 'Texas', 'Utah', 'Vermont', 'Virginia', 'Washington', 'West Virginia', 'Wisconsin',
    'Wyoming', 'Puerto Rico'];
  var USL = {}; US.forEach(function (s) { USL[s.toLowerCase()] = s; });
  USL['washington dc'] = USL['washington, dc'] = USL['washington, d.c.'] = USL['dc'] = 'District of Columbia';
  var VD = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  var ID = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
  var open = null, uid = 0;
  var hoverOK = window.matchMedia ? matchMedia('(hover: hover) and (pointer: fine)') : {matches: false};

  function bare(t) { return String(t || '').replace(/\s*\([\d,.\s]+\)\s*$/, '').replace(/\s+/g, ' ').trim(); }
  function stateOf(o) {
    if (o.getAttribute('data-st')) return o.getAttribute('data-st');
    var v = o.value, lab = bare(o.textContent);
    if (v === '' || v === '-' || /^(choose|pick|select|not in any|all |every )/i.test(lab)) return null;
    if (MAP.keys[v]) return MAP.keys[v];
    if (MAP.names[lab]) return MAP.names[lab];
    if (USL[lab.toLowerCase()]) return USL[lab.toLowerCase()];
    if (MAP.keys[String(v).toLowerCase()]) return MAP.keys[String(v).toLowerCase()];
    return MAP.fallback || 'Texas';   // same rule as the site's builder (procedure_pages.area_state): a place that isn't a state is a Texas metro
  }

  // -------- model: the select's options grouped by state
  function model(sel) {
    var groups = {}, order = [], head = [], tail = [], seenArea = false;
    Array.prototype.forEach.call(sel.options, function (o) {
      if (o.hidden && o.value === '') return;
      var st = stateOf(o);
      if (!st) { (seenArea ? tail : head).push(o); return; }
      seenArea = true;
      if (!groups[st]) { groups[st] = []; order.push(st); }
      groups[st].push(o);
    });
    order.sort(function (a, b) { return a.localeCompare(b); });
    // places A to Z on every page (the Price Finder's list came in build order); "Small towns of Texas" and the like go last
    var last = function (o) { return /^(small towns|rest of|other)/i.test(bare(o.textContent)) ? 1 : 0; };
    order.forEach(function (st) { groups[st].sort(function (a, b) { return last(a) - last(b) || bare(a.textContent).localeCompare(bare(b.textContent)); }); });
    return {groups: groups, states: order, head: head, tail: tail};
  }

  // -------- styles (one sheet; the button copies the look of the select it replaces, so it fits each page and theme)
  var CSS = '.am-native{position:absolute!important;width:1px!important;height:1px!important;opacity:0!important;pointer-events:none!important;overflow:hidden!important;clip:rect(0 0 0 0)!important;margin:0!important}' +
    '.am-btn{display:inline-flex;align-items:center;justify-content:space-between;gap:10px;text-align:left;cursor:pointer;max-width:100%;min-width:0;-webkit-appearance:none;appearance:none}' +
    '.am-btn>.am-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}' +
    '.am-btn>.am-c{flex:none;width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-top:6px solid currentColor;opacity:.75}' +
    '.am-btn:focus-visible{outline:3px solid var(--stamp,var(--brick,#9b2c1f));outline-offset:2px}' +
    '.am-btn[disabled]{opacity:.55;cursor:default}' +
    '.am-pop{position:fixed;z-index:2147483000;overflow:auto;overscroll-behavior:contain;padding:6px 0;border:1px solid rgba(0,0,0,.28);box-shadow:0 10px 28px rgba(0,0,0,.22);font-size:16px;line-height:1.3;border-radius:2px;-webkit-overflow-scrolling:touch}' +
    '.am-pop:focus{outline:0}' +
    '.am-it{display:flex;align-items:center;justify-content:space-between;gap:14px;min-height:40px;padding:8px 14px;cursor:pointer;user-select:none;-webkit-user-select:none;border-left:3px solid transparent;white-space:nowrap}' +
    '.am-it:hover,.am-it.am-on,.am-it[aria-expanded="true"]{background:rgba(127,127,127,.16)}' +
    '.am-it:focus{outline:0}.am-it:focus-visible{box-shadow:inset 0 0 0 2px var(--stamp,var(--brick,#9b2c1f))}' +
    '.am-it[aria-checked="true"],.am-it.am-cur{font-weight:700;border-left-color:var(--stamp,var(--brick,#9b2c1f))}' +
    '.am-n{font-size:.82em;opacity:.7;font-weight:400}' +
    '.am-ar{display:inline-block;width:7px;height:7px;border-right:2px solid currentColor;border-top:2px solid currentColor;transform:rotate(45deg);margin-left:6px;opacity:.7}' +
    '.am-it[aria-expanded="true"] .am-ar.am-dn{transform:rotate(135deg)}' +
    '.am-ar.am-dn{transform:rotate(135deg)}' +
    '.am-sep{height:1px;margin:6px 0;background:rgba(127,127,127,.35)}' +
    '.am-in{padding:2px 0 6px}.am-in .am-it{padding-left:30px;min-height:38px}' +
    '@media (prefers-reduced-motion:no-preference){.am-pop{animation:am-in .09s ease-out}}@keyframes am-in{from{opacity:0;transform:translateY(-3px)}}';
  function sheet() {
    if (document.getElementById('am-css')) return;
    var s = document.createElement('style'); s.id = 'am-css'; s.textContent = CSS; (document.head || document.documentElement).appendChild(s);
  }
  var COPY = ['font-family', 'font-size', 'font-weight', 'font-style', 'line-height', 'letter-spacing', 'text-transform', 'color',
    'background-color', 'background-image', 'background-position', 'background-repeat', 'background-size', 'box-sizing', 'box-shadow',
    'border-top-width', 'border-top-style', 'border-top-color', 'border-right-width', 'border-right-style', 'border-right-color',
    'border-bottom-width', 'border-bottom-style', 'border-bottom-color', 'border-left-width', 'border-left-style', 'border-left-color',
    'border-top-left-radius', 'border-top-right-radius', 'border-bottom-left-radius', 'border-bottom-right-radius',
    'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'min-height', 'height',
    'margin-top', 'margin-right', 'margin-bottom', 'margin-left', 'flex-grow', 'flex-shrink', 'flex-basis', 'align-self', 'vertical-align'];
  function restyle(I) {
    var sel = I.sel, b = I.btn;
    sel.classList.remove('am-native'); b.style.display = 'none';
    var cs = getComputedStyle(sel), par = sel.parentElement, w = sel.getBoundingClientRect().width, pw = 0;
    if (par) { var pc = getComputedStyle(par); pw = par.getBoundingClientRect().width - parseFloat(pc.paddingLeft) - parseFloat(pc.paddingRight); }
    var st = {};
    COPY.forEach(function (p) { st[p] = cs.getPropertyValue(p); });
    var disp = cs.display;
    sel.classList.add('am-native'); b.style.display = '';
    b.style.cssText = '';
    COPY.forEach(function (p) { if (st[p]) b.style.setProperty(p, st[p]); });
    if (disp === 'block' || (pw && w >= pw - 2)) b.style.width = '100%';
    else if (w) b.style.width = Math.round(w) + 'px';
    if (st['flex-basis'] && st['flex-basis'] !== 'auto') b.style.width = '';
    if (disp === 'block') b.style.display = 'flex';
    I.caret.style.display = st['background-image'] && st['background-image'] !== 'none' ? 'none' : '';
  }
  function solid(c) { var m = /rgba?\(([^)]+)\)/.exec(c || ''); if (!m) return !!c && c !== 'transparent'; var p = m[1].split(','); return p.length < 4 || parseFloat(p[3]) > 0.85; }
  function popColors(I) {
    var cs = getComputedStyle(I.btn), bg = cs.backgroundColor, el = I.btn.parentElement;
    while (!solid(bg) && el) { bg = getComputedStyle(el).backgroundColor; el = el.parentElement; }
    if (!solid(bg)) bg = '#fff';
    var fg = cs.color;   // a picker set in white on a dark header gets a light menu: never pale text on a light menu
    if (contrast(fg, bg) < 4.5) fg = lum(bg) > 0.35 ? '#1d1a16' : '#ffffff';
    return {bg: bg, fg: fg, font: cs.fontFamily};
  }
  function rgb(c) { var m = /rgba?\(([^)]+)\)/.exec(c || ''); if (m) return m[1].split(',').slice(0, 3).map(parseFloat); if (/^#[0-9a-f]{6}$/i.test(c)) return [1, 3, 5].map(function (i) { return parseInt(c.substr(i, 2), 16); }); return [255, 255, 255]; }
  function lum(c) { return rgb(c).map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }).reduce(function (s, v, i) { return s + v * [0.2126, 0.7152, 0.0722][i]; }, 0); }
  function contrast(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }

  // -------- one picker
  function enhance(sel) {
    if (sel.__am || sel.multiple || sel.matches(SKIP)) return;
    sheet();
    var b = document.createElement('button'); b.type = 'button'; b.className = 'am-btn';
    b.setAttribute('aria-haspopup', 'menu'); b.setAttribute('aria-expanded', 'false');
    var t = document.createElement('span'); t.className = 'am-t';
    var c = document.createElement('span'); c.className = 'am-c'; c.setAttribute('aria-hidden', 'true');
    b.appendChild(t); b.appendChild(c);
    var I = {sel: sel, btn: b, text: t, caret: c};
    sel.__am = I;
    sel.parentNode.insertBefore(b, sel.nextSibling);
    restyle(I);
    sel.setAttribute('tabindex', '-1'); sel.setAttribute('aria-hidden', 'true');
    // the label: point it at the button
    var lab = (sel.id && document.querySelector('label[for="' + sel.id + '"]')) || sel.closest('label');
    if (lab) {
      if (!lab.id) lab.id = 'am-l' + (++uid);
      var own = sel.closest('label') === lab;
      if (own) b.setAttribute('aria-label', bare(lab.textContent.replace(t.textContent, '')) || sel.getAttribute('aria-label') || 'Area');
      else b.setAttribute('aria-labelledby', lab.id);
      lab.addEventListener('click', function (e) { if (!b.contains(e.target)) { e.preventDefault(); b.focus(); } });
    } else b.setAttribute('aria-label', sel.getAttribute('aria-label') || 'Area');
    // keep the button in step with the select, whoever changes it
    try {
      Object.defineProperty(sel, 'value', {configurable: true, get: function () { return VD.get.call(this); }, set: function (v) { VD.set.call(this, v); label(I); }});
      Object.defineProperty(sel, 'selectedIndex', {configurable: true, get: function () { return ID.get.call(this); }, set: function (v) { ID.set.call(this, v); label(I); }});
    } catch (e) {}
    new MutationObserver(function () { label(I); if (open === I) close(); }).observe(sel, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['selected', 'disabled', 'label']});
    sel.addEventListener('change', function () { label(I); });
    b.addEventListener('click', function () { open === I ? close(true) : show(I); });
    b.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || ((e.key === 'Enter' || e.key === ' ') && open !== I)) { e.preventDefault(); show(I, true); }
    });
    label(I);
  }
  function current(I) { var s = I.sel, i = ID.get.call(s); return i >= 0 ? s.options[i] : null; }
  function label(I) {
    var o = current(I), m = model(I.sel), txt;
    if (!o) txt = I.sel.options.length ? 'Choose an area' : '\u2026';
    else {
      var st = stateOf(o), many = st && m.groups[st] && m.groups[st].length > 1;
      txt = many && bare(o.textContent) !== st ? st + ' \u00b7 ' + o.textContent.trim() : o.textContent.trim();
    }
    I.text.textContent = txt;
    I.btn.title = txt;
    I.btn.disabled = !!I.sel.disabled;
  }

  // -------- the open menu
  var P = null;   // {I, pop, sub, subFor, flyout, timers}
  function el(tag, cls, txt) { var e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  function leaf(o, cur) {
    var it = el('div', 'am-it'); it.setAttribute('role', 'menuitemradio'); it.setAttribute('tabindex', '-1');
    it.setAttribute('aria-checked', String(o === cur)); it.appendChild(el('span', '', o.textContent.trim()));
    if (o.disabled) it.setAttribute('aria-disabled', 'true');
    it.__opt = o; return it;
  }
  function show(I, kb) {
    close();
    var m = model(I.sel), cur = current(I), curSt = cur && stateOf(cur), col = popColors(I);
    var pop = el('div', 'am-pop'); pop.setAttribute('role', 'menu'); pop.setAttribute('aria-label', I.btn.getAttribute('aria-label') || 'Area');
    pop.style.background = col.bg; pop.style.color = col.fg; pop.style.fontFamily = col.font;
    m.head.forEach(function (o) { pop.appendChild(leaf(o, cur)); });
    if (m.head.length && m.states.length) pop.appendChild(el('div', 'am-sep'));
    m.states.forEach(function (st) {
      var opts = m.groups[st];
      if (opts.length === 1) {
        var it = leaf(opts[0], cur); it.firstChild.textContent = st;
        if (bare(opts[0].textContent) !== st) it.appendChild(el('span', 'am-n', opts[0].textContent.trim()));
        pop.appendChild(it); return;
      }
      var p = el('div', 'am-it'); p.setAttribute('role', 'menuitem'); p.setAttribute('tabindex', '-1');
      p.setAttribute('aria-haspopup', 'menu'); p.setAttribute('aria-expanded', 'false');
      if (st === curSt) p.classList.add('am-cur');
      p.appendChild(el('span', '', st));
      var n = el('span', 'am-n', opts.length + ' places'); n.appendChild(el('i', 'am-ar')); p.appendChild(n);
      p.__st = st; p.__opts = opts; pop.appendChild(p);
    });
    if (m.tail.length) { pop.appendChild(el('div', 'am-sep')); m.tail.forEach(function (o) { pop.appendChild(leaf(o, cur)); }); }
    document.body.appendChild(pop);
    P = {I: I, pop: pop, sub: null, subFor: null, flyout: window.innerWidth >= 700, cur: cur, col: col, t: 0};
    if (!P.flyout) pop.querySelectorAll('.am-ar').forEach(function (a) { a.classList.add('am-dn'); });
    open = I; I.btn.setAttribute('aria-expanded', 'true');
    place();
    wire(pop);
    var focusOn = pop.querySelector('.am-cur') || pop.querySelector('[aria-checked="true"]') || pop.querySelector('.am-it');
    if (focusOn) { focusOn.scrollIntoView({block: 'nearest'}); if (kb) focusOn.focus(); else pop.setAttribute('tabindex', '-1'); }
    if (!kb) pop.focus({preventScroll: true});
  }
  function place() {
    if (!P) return;
    var r = P.I.btn.getBoundingClientRect(), pop = P.pop, vw = window.innerWidth, vh = window.innerHeight, gap = 4;
    var below = vh - r.bottom - 12, above = r.top - 12, up = below < 260 && above > below;
    var maxH = Math.max(160, Math.min(440, up ? above : below));
    pop.style.maxHeight = maxH + 'px';
    // a menu, not a sheet: a full-width picker (control center Cash prices) still gets a list about 360px wide, so the
    // places can open beside it instead of on top of it
    var w = P.flyout ? Math.min(Math.max(r.width, 260), 360) : Math.max(r.width, Math.min(vw - 24, 300));
    pop.style.minWidth = Math.min(w, vw - 16) + 'px'; pop.style.maxWidth = (P.flyout ? Math.max(w, 420) : vw - 16) + 'px';
    var pw = pop.offsetWidth, ph = pop.offsetHeight;
    pop.style.left = Math.max(8, Math.min(r.left, vw - pw - 8)) + 'px';
    pop.style.top = (up ? Math.max(8, r.top - gap - ph) : r.bottom + gap) + 'px';
    if (P.sub && P.flyout) placeSub();
  }
  function placeSub() {
    var row = P.subFor, sub = P.sub, pr = P.pop.getBoundingClientRect(), rr = row.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    sub.style.maxHeight = (vh - 16) + 'px';
    var sw = sub.offsetWidth, sh = sub.offsetHeight;
    var left = pr.right - 2;
    if (left + sw > vw - 8) left = pr.left - sw + 2 >= 8 ? pr.left - sw + 2 : Math.max(8, vw - sw - 8);   // right, else left, else overlap the right edge
    sub.style.left = left + 'px';
    sub.style.top = Math.max(8, Math.min(rr.top - 7, vh - sh - 8)) + 'px';
  }
  function openSub(row, focusFirst) {
    if (P.subFor === row) { if (focusFirst) first(P.sub); return; }
    closeSub();
    var cur = P.cur, sub;
    if (P.flyout) {
      sub = el('div', 'am-pop'); sub.setAttribute('role', 'menu'); sub.setAttribute('aria-label', row.__st);
      sub.style.background = P.col.bg; sub.style.color = P.col.fg; sub.style.fontFamily = P.col.font;
      row.__opts.forEach(function (o) { sub.appendChild(leaf(o, cur)); });
      document.body.appendChild(sub); P.sub = sub; P.subFor = row; placeSub(); wire(sub);
      sub.addEventListener('mouseenter', function () { clearTimeout(P.t); });
    } else {
      sub = el('div', 'am-in'); sub.setAttribute('role', 'menu'); sub.setAttribute('aria-label', row.__st);
      row.__opts.forEach(function (o) { sub.appendChild(leaf(o, cur)); });
      row.parentNode.insertBefore(sub, row.nextSibling); P.sub = sub; P.subFor = row; wire(sub);
      var pr = P.pop.getBoundingClientRect(), sr = sub.getBoundingClientRect();
      if (sr.bottom > pr.bottom) P.pop.scrollTop += Math.min(sr.bottom - pr.bottom + 8, row.getBoundingClientRect().top - pr.top);
    }
    row.setAttribute('aria-expanded', 'true');
    if (focusFirst) first(sub);
  }
  function first(menu) { var a = menu.querySelector('[aria-checked="true"]') || menu.querySelector('.am-it'); if (a) { a.focus(); a.scrollIntoView({block: 'nearest'}); } }
  function closeSub() {
    if (!P || !P.sub) return;
    P.subFor.setAttribute('aria-expanded', 'false');
    P.sub.remove(); P.sub = null; P.subFor = null;
  }
  function close(refocus) {
    if (!P) { open = null; return; }
    clearTimeout(P.t);
    var I = P.I;
    closeSub(); P.pop.remove(); P = null; open = null;
    I.btn.setAttribute('aria-expanded', 'false');
    if (refocus) I.btn.focus();
  }
  function pick(o) {
    var I = P.I;
    close(true);
    if (o.disabled) return;
    var before = VD.get.call(I.sel);
    VD.set.call(I.sel, o.value); label(I);
    if (before !== o.value) {
      I.sel.dispatchEvent(new Event('input', {bubbles: true}));
      I.sel.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
  function items(menu) {
    var out = [];
    Array.prototype.forEach.call(menu.children, function (c) { if (c.classList.contains('am-it')) out.push(c); else if (c.classList.contains('am-in')) Array.prototype.push.apply(out, c.querySelectorAll('.am-it')); });
    return out;
  }
  function wire(menu) {
    menu.addEventListener('click', function (e) {
      var it = e.target.closest('.am-it'); if (!it || !P) return;
      if (it.__opt) pick(it.__opt);
      else if (P.subFor === it && !P.flyout) closeSub();
      else openSub(it, false);
    });
    menu.addEventListener('mouseover', function (e) {
      if (!P || !P.flyout || !hoverOK.matches) return;
      var it = e.target.closest('.am-it'); if (!it || menu !== P.pop) return;
      clearTimeout(P.t);
      P.t = setTimeout(function () { if (!P) return; if (it.__st) openSub(it, false); else closeSub(); }, it.__st ? 90 : 200);
    });
    menu.addEventListener('mouseleave', function (e) {
      if (!P || !P.flyout || !hoverOK.matches || menu !== P.pop) return;
      if (P.sub && e.relatedTarget && P.sub.contains(e.relatedTarget)) return;
      clearTimeout(P.t); P.t = setTimeout(function () { if (P) closeSub(); }, 350);
    });
    menu.addEventListener('keydown', function (e) {
      if (!P) return;
      var inSub = P.sub && P.sub.contains(document.activeElement) && P.flyout;
      var list = items(inSub ? P.sub : P.pop), a = document.activeElement, i = list.indexOf(a), k = e.key;
      if (k === 'ArrowDown' || k === 'ArrowUp') { e.preventDefault(); var j = i < 0 ? 0 : (i + (k === 'ArrowDown' ? 1 : -1) + list.length) % list.length; list[j].focus(); list[j].scrollIntoView({block: 'nearest'}); }
      else if (k === 'Home' || k === 'End') { e.preventDefault(); var x = list[k === 'Home' ? 0 : list.length - 1]; if (x) x.focus(); }
      else if (k === 'ArrowRight' && a && a.__st) { e.preventDefault(); openSub(a, true); }
      else if ((k === 'ArrowLeft' || k === 'Escape') && P.sub && (inSub || (P.sub.contains(a)))) { e.preventDefault(); var row = P.subFor; closeSub(); row.focus(); }
      else if (k === 'Escape') { e.preventDefault(); close(true); }
      else if (k === 'Enter' || k === ' ') { e.preventDefault(); if (a && a.__opt) pick(a.__opt); else if (a && a.__st) openSub(a, true); }
      else if (k === 'Tab') close(false);
      else if (k.length === 1 && /\S/.test(k)) {
        var L = k.toLowerCase(), n = list.length;
        for (var s = 1; s <= n; s++) { var c = list[((i < 0 ? -1 : i) + s) % n]; if (c.textContent.trim().toLowerCase().indexOf(L) === 0) { c.focus(); c.scrollIntoView({block: 'nearest'}); break; } }
      }
    });
  }
  document.addEventListener('pointerdown', function (e) {
    if (!P) return;
    if (P.pop.contains(e.target) || (P.sub && P.sub.contains(e.target)) || P.I.btn.contains(e.target)) return;
    close(false);
  }, true);
  window.addEventListener('resize', function () { if (P) close(false); });
  window.addEventListener('scroll', function (e) { if (P && !(P.pop.contains(e.target) || (P.sub && P.sub.contains(e.target)))) place(); }, true);

  // -------- find pickers now and as pages add them (the Price Finder adds its picker after cities.json loads;
  // the control center draws its tabs on demand)
  var all = [];
  function scan() {
    document.querySelectorAll(SEL).forEach(function (s) { if (s.tagName === 'SELECT' && !s.__am) { enhance(s); if (s.__am) all.push(s.__am); } });
  }
  var queued = false;
  function later() { if (!queued) { queued = true; requestAnimationFrame(function () { queued = false; scan(); }); } }
  function start() {
    scan();
    new MutationObserver(function (ms) {
      for (var i = 0; i < ms.length; i++) if (ms[i].addedNodes.length) { later(); break; }
    }).observe(document.documentElement, {childList: true, subtree: true});
    // a look or theme change (control center) re-reads each select's look
    var re = function () { all.forEach(function (I) { if (I.sel.isConnected) { restyle(I); label(I); } }); };
    new MutationObserver(function () { requestAnimationFrame(re); }).observe(document.documentElement, {attributes: true, attributeFilter: ['data-look', 'data-theme', 'class', 'style']});
    if (document.head) new MutationObserver(function () { setTimeout(re, 60); }).observe(document.head, {childList: true});
    window.addEventListener('load', re);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(re);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
