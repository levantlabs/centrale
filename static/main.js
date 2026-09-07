// static/main.js -- the top-level render orchestration and the boot call
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Top-level render orchestration
  // ------------------------------------------------------------------

  // task-81: the open drawer's `summary` is the board task it was
  // opened from; re-point it at the same task in the freshly fetched
  // board so everything the drawer derives from board data
  // (branchCheckout -> the disabled external Merge / "Worked
  // externally" state, agentState, ready, ...) follows a refetch
  // exactly as the card does -- no reload, no new client state. A task
  // that vanished from the board (completed/ archive) keeps its last
  // snapshot, as before.
  function syncDrawerSummary() {
    if (!C.currentDrawer) return;
    var fresh = C.findTask(C.currentDrawer.project, C.currentDrawer.id);
    if (fresh) C.currentDrawer.summary = fresh;
  }

  function renderAll() {
    C.renderProjectChips();
    C.renderErrorBanners();
    // task-86: before renderBoard, so a milestone that appeared (or a
    // project toggled on/off) is in the dropdown by the time the lanes
    // it filters are rebuilt.
    C.renderMilestoneFilter();
    C.renderBoard();
    C.renderSessionsPanel();
    syncDrawerSummary();
    if (C.currentDrawer) { C.renderDrawerSpawnArea(); C.renderDrawerHarvestArea(); C.renderDrawerSessionArea(); }
    // task-60: (re)evaluate whether the open drawer's task has a live
    // session / the feature is on -- starts, keeps, or stops the pane
    // poll accordingly. Deliberately NOT a render of the pane itself:
    // the pane area is only ever written by the poller's own responses.
    C.syncDrawerPanePolling();
  }

  // task-50: the fetch-driven refresh path's own entry point -- called
  // after EVERY fetchBoard/fetchSessions resolution (success or
  // failure), never from anything else. Renders everything renderAll()
  // does, but only when the freshly fetched board+sessions+error state
  // actually differs from what's already on screen; an unchanged
  // refresh (the common case on a quiet board) leaves the DOM
  // completely untouched -- no flicker, no lanes reset to scrollTop 0,
  // no lost keyboard focus.
  //
  // Every OTHER render trigger -- a confirm-arm window expiring
  // (refreshSpawnButtons/refreshHarvestButtons), a spawn/harvest/
  // cleanup/end-session success, search/filter/theme changes, auto-
  // harvest event toasts -- still calls renderBoard()/renderSessionsPanel()
  // (or renderAll()) directly and unconditionally, exactly as before
  // task-50; none of those go through this function or its skip check,
  // so nothing here can freeze or delay them.
  function renderBoardAndSessionsIfChanged() {
    var payload = JSON.stringify({ board: C.boardData, sessions: C.sessionsData, error: C.lastBoardError });
    if (payload === C.lastRenderedFetchPayload) return;
    C.lastRenderedFetchPayload = payload;
    renderAll();
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.renderAll = renderAll;
  C.renderBoardAndSessionsIfChanged = renderBoardAndSessionsIfChanged;

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  C.doRefresh(true);
})(window.Centrale = window.Centrale || {});
