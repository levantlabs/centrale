// static/dom.js -- the tiny DOM builder and the per-project colour helpers
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Small DOM helpers
  // ------------------------------------------------------------------

  function h(tag, opts) {
    opts = opts || {};
    var el = document.createElement(tag);
    if (opts.className) el.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) el.textContent = opts.text;
    if (opts.title) el.title = opts.title;
    if (opts.attrs) {
      for (var k in opts.attrs) {
        if (Object.prototype.hasOwnProperty.call(opts.attrs, k)) {
          el.setAttribute(k, opts.attrs[k]);
        }
      }
    }
    if (opts.onclick) el.addEventListener("click", opts.onclick);
    if (opts.children) {
      opts.children.forEach(function (c) { if (c) el.appendChild(c); });
    }
    return el;
  }

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function byId(id) { return document.getElementById(id); }

  // ------------------------------------------------------------------
  // Hashing / color helpers
  // ------------------------------------------------------------------

  function hashString(str) {
    var hash = 0;
    for (var i = 0; i < str.length; i++) {
      hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
    }
    return hash;
  }

  function projectHue(name) {
    return hashString(String(name)) % 360;
  }

  function isDarkTheme() {
    return !!(window.CentraleTheme && window.CentraleTheme.current() === "dark");
  }

  function projectChipStyle(name) {
    var hue = projectHue(name);
    if (isDarkTheme()) {
      return {
        background: "hsl(" + hue + ", 46%, 18%)",
        color: "hsl(" + hue + ", 85%, 78%)",
        border: "1px solid hsl(" + hue + ", 46%, 32%)"
      };
    }
    // Light theme: pale tinted background with darker, saturated text --
    // the same "muted chip" convention as the priority pills.
    return {
      background: "hsl(" + hue + ", 65%, 94%)",
      color: "hsl(" + hue + ", 55%, 32%)",
      border: "1px solid hsl(" + hue + ", 50%, 80%)"
    };
  }

  function applyStyle(el, styleObj) {
    for (var k in styleObj) {
      if (Object.prototype.hasOwnProperty.call(styleObj, k)) {
        el.style[k] = styleObj[k];
      }
    }
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.applyStyle = applyStyle;
  C.byId = byId;
  C.clearChildren = clearChildren;
  C.h = h;
  C.isDarkTheme = isDarkTheme;
  C.projectChipStyle = projectChipStyle;
})(window.Centrale = window.Centrale || {});
