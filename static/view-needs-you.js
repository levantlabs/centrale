// static/view-needs-you.js -- task-188: the optional human attention inbox.
// Its script tag is the only integration point. Evidence comes from the
// shared fleet snapshot; this view owns no poller and stores no task state.
(function (C) {
  "use strict";

  var pending = {};                 // only input requests currently in flight
  var messages = {};                // transient input outcomes, never verdicts
  var labels = {permission: "Permission or menu", merge: "Merge needs recheck",
    message: "Held or undelivered message", idle: "Agent may need input", owner: "Owner question"};

  function key(item) { return item.kind + ":" + item.project + ":" + item.taskId; }
  function items(snapshot) { return snapshot && snapshot.needsYou || []; }
  function age(since) {
    if (since === null || since === undefined) return "wait duration unknown";
    var seconds = Math.max(0, Math.floor(Date.now() / 1000 - since));
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    return Math.floor(seconds / 3600) + "h " + Math.floor(seconds % 3600 / 60) + "m";
  }

  function blockReason(item, snapshot, error) {
    if (pending[key(item)]) return "Sending…";
    if (error) return "Snapshot unavailable; wait for a fresh capture";
    if (snapshot.sessionPreviewMode !== "interact") return "Enable session interaction in Settings to answer";
    if (!item.capturedAt || Date.now() / 1000 - item.capturedAt > 10) return "Capture is stale; wait for a fresh one";
    return null;
  }

  function redraw() {
    if (C.views.current() === "needs-you") render(C.byId("view-host"), C.fleetSnapshot, C.fleetError);
  }

  function answer(item, index) {
    if (blockReason(item, C.fleetSnapshot || {}, C.fleetError)) return;
    var id = key(item);
    pending[id] = true;
    delete messages[id];
    redraw();
    // Refresh through the ONE shared poller, then compare the actual screen
    // reviewed by the click. A moved highlight or changed question requires
    // another review, never a guessed answer to a different permission.
    C.fleetData.refresh().then(function () {
      var fresh = items(C.fleetSnapshot).filter(function (candidate) { return key(candidate) === id; })[0];
      if (C.fleetError) throw new Error(C.fleetError);
      if (!fresh) return;
      if (JSON.stringify(fresh.lines) !== JSON.stringify(item.lines) || fresh.since !== item.since) {
        throw new Error("The dialog changed. Review the fresh menu before answering.");
      }
      // Temporarily omit the in-flight marker from this freshness check.
      pending[id] = false;
      var reason = blockReason(fresh, C.fleetSnapshot, C.fleetError);
      pending[id] = true;
      if (reason) throw new Error(reason);
      var keys = C.menuKeysForClick(fresh.lines, index);
      if (!keys) throw new Error("The menu could not be read. Open the task to inspect it.");
      return C.sendSessionKeys(item.project, item.taskId, keys);
    }).then(function () {
      return C.fleetData.refresh();
    }).catch(function (err) {
      messages[id] = "Not sent: " + (err.message || err);
    }).then(function () {
      delete pending[id];
      redraw();
    });
  }

  // task-190: cards persist between polls, so a new item can slide in and an
  // answered one slide out, and an untouched card is left alone rather than
  // rebuilt (which would restart its animation and drop hover/focus).
  var root = null, top = null, list = null;
  var cards = {};       // key -> {el, sig, leaving}
  var primed = false;   // the first paint of a shown view doesn't animate every card in

  function isOwnControl(ev, card) {
    var node = ev && ev.target;
    while (node && node !== card) {
      if (node.tagName && /^(BUTTON|A|INPUT|SELECT|TEXTAREA|PRE)$/i.test(node.tagName)) return true;
      node = node.parentNode;
    }
    return false;
  }

  function openTask(item) {
    var task = C.findTask(item.project, item.taskId);
    if (!task) {
      C.showToast(item.taskId + " is not on the board right now; refresh the board and try again.", "error");
      return;
    }
    C.openDrawer({ name: item.project }, task);
  }

  // task-194: the mockup's card. A coloured left edge per kind, the kind as a
  // caps label, a header line (project hue dot, id, agent tag), the title and
  // detail, the evidence as "Detected from" chips, and the age top right over
  // an urgency bar that fills as the item waits. Icons stay out.
  var details = {permission: "Pick an answer. It goes to the agent as arrow keys and Enter.",
    merge: "The last merge attempt did not go through.",
    message: "Centrale could not deliver this message.",
    idle: "The agent stopped without reporting finished. It may be asking something in plain text.",
    owner: "A question labeled for the owner."};
  var WAIT_CAP = 30 * 60;   // seconds of waiting that fill the urgency bar

  function urgency(since) {
    if (since === null || since === undefined) return 0;
    return Math.min(100, Math.max(0, Math.round(100 * (Date.now() / 1000 - since) / WAIT_CAP)));
  }

  function chips(signal) {
    var box = C.h("div", {className: "needs-you-signal"});
    box.appendChild(C.h("span", {text: "Detected from"}));
    String(signal || "").split(" + ").forEach(function (part, i) {
      if (i) box.appendChild(C.h("span", {className: "needs-you-plus", text: "+"}));
      if (part.trim()) box.appendChild(C.h("span", {className: "needs-you-chip", text: part.trim()}));
    });
    return box;
  }

  function terminal(item, reason) {
    var term = C.h("div", {className: "needs-you-term"});
    (item.lines || []).forEach(function (line, index) {
      if (!C.menuKeysForClick(item.lines, index)) {
        term.appendChild(C.h("div", {className: "needs-you-term-line", text: line}));
        return;
      }
      var row = C.h("button", {className: "needs-you-opt" + (/^\s*\u203a/.test(line) ? " sel" : ""), text: line.trim(),
        attrs: {type: "button"}, onclick: function () { answer(item, index); }});
      row.disabled = !!reason;
      if (reason) row.title = reason;
      term.appendChild(row);
    });
    return term;
  }

  function fillCard(card, item, snapshot, error, reason) {
    var id = key(item);
    var el = card.el;
    var hue = C.projectHue(item.project);
    C.clearChildren(el);
    if (el.style.setProperty) el.style.setProperty("--p", "hsl(" + hue + ", 62%, 52%)"); else el.style["--p"] = "hsl(" + hue + ", 62%, 52%)";
    var head = C.h("div", {className: "needs-you-head", children: [
      C.h("span", {className: "needs-you-kind", text: labels[item.kind] || item.kind}),
      C.h("span", {className: "needs-you-identity", children: [C.h("i", {className: "needs-you-dot", attrs: {"aria-hidden": "true"}}),
        C.h("span", {text: item.project + " \u00b7 " + item.taskId})]}),
      C.h("span", {className: "needs-you-agent", text: item.agent})]});
    var body = C.h("div", {className: "needs-you-body", children: [head,
      C.h("h3", {className: "needs-you-title", text: item.title || labels[item.kind] || item.kind}),
      C.h("p", {className: "needs-you-detail", text: details[item.kind] || ""})]});
    if (item.text) body.appendChild(C.h("blockquote", {className: "needs-you-text", text: item.text}));
    if (item.kind === "permission") {
      body.appendChild(terminal(item, reason));
      if (reason) body.appendChild(C.h("p", {className: "needs-you-reason", text: reason}));
      if (!(item.lines || []).some(function (l, i) { return C.menuKeysForClick(item.lines, i); })) {
        body.appendChild(C.h("p", {className: "needs-you-reason", text: "No numbered menu detected. Open the task to answer in the session pane."}));
      }
    }
    if (messages[id]) body.appendChild(C.h("p", {className: "view-error", attrs: {role: "status"}, text: messages[id]}));
    body.appendChild(C.h("div", {className: "needs-you-actions", children: [
      C.h("button", {className: "btn", text: "Open task", attrs: {type: "button"}, onclick: function () { openTask(item); }})]}));
    body.appendChild(chips(item.signal));
    var bar = C.h("div", {className: "needs-you-urgency", attrs: {title: "How long it has waited, out of 30 min"}, children: [C.h("div")]});
    bar.firstChild.style.width = urgency(item.since) + "%";
    el.appendChild(body);
    el.appendChild(C.h("div", {className: "needs-you-side", children: [C.h("span", {className: "needs-you-age", text: age(item.since)}), bar]}));
  }

  function makeCard(item) {
    var card = {el: C.h("article", {className: "needs-you-item ny-k-" + item.kind}), sig: null, leaving: null};
    card.el.addEventListener("click", function (ev) {
      if (isOwnControl(ev, card.el)) return;
      var sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length) return;   // selecting text to copy, not opening
      openTask(card.item);
    });
    return card;
  }

  function retire(id, card) {
    if (card.leaving) return;
    var el = card.el;
    el.style.maxHeight = (el.offsetHeight || 0) + "px";
    C.replayClass(el, "leaving");
    // A timer, not transitionend: reduced motion drops the transition and
    // the card must still go.
    card.leaving = setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
      if (cards[id] === card) delete cards[id];
    }, 450);
  }

  function render(host, snapshot, error) {
    if (!root || root.parentNode !== host) {
      C.clearChildren(host);
      top = C.h("div", {className: "needs-you-top"});
      list = C.h("div", {className: "needs-you-list"});
      root = C.h("div", {className: "needs-you-root", children: [top, list]});
      host.appendChild(root);
      cards = {};
      primed = false;
    }
    C.clearChildren(top);
    top.appendChild(C.h("p", {className: "needs-you-aside",
      text: "Only things a person has to do. Finished work awaiting review stays with the project's master."}));
    if (error) top.appendChild(C.h("p", {className: "view-error", text: "Inbox unavailable: " + error + ". Any items below are stale."}));
    if (!snapshot) {
      if (!error) top.appendChild(C.h("p", {text: "Loading attention signals…"}));
      return;
    }
    var warnings = (snapshot.needsYouErrors || []).slice();
    if (snapshot.sessionPreviewMode === "off") warnings.push("Permission checks are disabled in Settings.");
    if (snapshot.deliveryError) warnings.push("Delivery history: " + snapshot.deliveryError);
    if (snapshot.historyError) warnings.push("Fleet history: " + snapshot.historyError);
    warnings.forEach(function (warning) { top.appendChild(C.h("p", {className: "view-error", text: warning})); });
    var rows = items(snapshot);
    if (!rows.length && !error) {
      top.appendChild(C.h("div", {className: "needs-you-empty", children: [
        C.h("span", {className: "needs-you-check", text: "\u2713", attrs: {"aria-hidden": "true"}}),
        C.h("b", {text: warnings.length ? "No items in the available signals" : "Nothing needs you right now"}),
        C.h("span", {text: warnings.length ? "Some checks failed above." : "New prompts and questions will slide in here."})]}));
    }
    var present = {};
    var nodes = [];
    rows.forEach(function (item) {
      var id = key(item);
      present[id] = true;
      var card = cards[id];
      if (!card) {
        card = cards[id] = makeCard(item);
        if (primed) card.el.classList.add("enter");
      } else if (card.leaving) {
        // The same item came back before its exit finished.
        clearTimeout(card.leaving);
        card.leaving = null;
        card.el.style.maxHeight = "";
        card.el.classList.remove("leaving");
      }
      card.item = item;
      var reason = item.kind === "permission" ? blockReason(item, snapshot, error) : null;
      var sig = JSON.stringify([item, reason, messages[id] || null, age(item.since)]);
      if (sig !== card.sig) { fillCard(card, item, snapshot, error, reason); card.sig = sig; }
      nodes.push(card.el);
    });
    // Cards on their way out stay where they were until their exit has
    // played; moving one would restart its animation.
    Object.keys(cards).forEach(function (id) {
      if (present[id]) return;
      retire(id, cards[id]);
      nodes.splice(Math.min(Math.max(Array.prototype.indexOf.call(list.childNodes, cards[id].el), 0), nodes.length), 0, cards[id].el);
    });
    C.syncChildren(list, nodes);
    primed = true;
    // Outcomes only belong to items still present; do not accumulate them.
    Object.keys(messages).forEach(function (id) { if (!present[id]) delete messages[id]; });
  }

  C.views.register({id: "needs-you", label: "Needs you", render: render,
    badge: function (snapshot) {
      if (C.fleetError) return "?";
      if (!snapshot) return null;
      var partial = (snapshot.needsYouErrors || []).length || snapshot.deliveryError ||
        snapshot.historyError || snapshot.sessionPreviewMode === "off";
      var count = items(snapshot).length;
      // Nothing to do and nothing unknown: no badge at all, not a "0".
      if (!count && !partial) return null;
      return String(count) + (partial ? "+" : "");
    }});
})(window.Centrale = window.Centrale || {});
