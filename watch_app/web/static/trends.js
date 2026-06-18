/* Interactivity for the /trends charts.
 *
 * Pure DOM glue over the server-rendered SVG — no library, no recompute. The
 * geometry and the per-point data (dates, counts, ranks, names) are emitted by
 * the server as JSON blobs; this file only listens for hover/click and moves a
 * tooltip, a marker, and opacity around. It no-ops if the chart elements are
 * absent (e.g. the empty-state trends page), so it is safe to always load.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  // Map a client (mouse) x into the SVG's viewBox x-coordinate.
  function viewBoxX(svg, clientX) {
    var rect = svg.getBoundingClientRect();
    var vb = svg.viewBox.baseVal;
    return vb.x + (clientX - rect.left) * (vb.width / rect.width);
  }

  // Position an absolutely-placed tooltip (inside `wrap`) centered above the
  // given point, expressed in viewBox coordinates.
  function placeTip(tip, wrap, svg, vbx, vby) {
    var rect = svg.getBoundingClientRect();
    var wrapRect = wrap.getBoundingClientRect();
    var vb = svg.viewBox.baseVal;
    var px = (vbx - vb.x) / vb.width * rect.width + (rect.left - wrapRect.left);
    var py = (vby - vb.y) / vb.height * rect.height + (rect.top - wrapRect.top);
    tip.style.left = px + "px";
    tip.style.top = py + "px";
    tip.style.transform = "translate(-50%, calc(-100% - 8px))";
  }

  function nearestByX(points, vbx) {
    var best = null, bestD = Infinity;
    for (var i = 0; i < points.length; i++) {
      var d = Math.abs(points[i].x - vbx);
      if (d < bestD) { bestD = d; best = points[i]; }
    }
    return best;
  }

  function parseJSON(el) {
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function initVolume() {
    var svg = document.getElementById("vol-svg");
    var wrap = document.getElementById("vol-wrap");
    var tip = document.getElementById("vol-tip");
    var guide = document.getElementById("vol-guide");
    var marker = document.getElementById("vol-marker");
    var points = parseJSON(document.getElementById("vol-data"));
    if (!svg || !wrap || !tip || !guide || !marker || !points || !points.length) return;

    svg.addEventListener("mousemove", function (ev) {
      var p = nearestByX(points, viewBoxX(svg, ev.clientX));
      if (!p) return;
      guide.setAttribute("x1", p.x);
      guide.setAttribute("x2", p.x);
      marker.setAttribute("cx", p.x);
      marker.setAttribute("cy", p.y);
      show(guide); show(marker);
      tip.textContent = p.label + " \u00b7 " + p.count +
        " mention" + (p.count === 1 ? "" : "s");
      placeTip(tip, wrap, svg, p.x, p.y);
      show(tip);
    });
    svg.addEventListener("mouseleave", function () {
      hide(tip); hide(guide); hide(marker);
    });
  }

  function initTrajectory() {
    var svg = document.getElementById("traj-svg");
    var wrap = document.getElementById("traj-wrap");
    var tip = document.getElementById("traj-tip");
    var marker = document.getElementById("traj-marker");
    var legend = document.getElementById("traj-legend");
    var lines = parseJSON(document.getElementById("traj-data"));
    if (!svg || !wrap || !tip || !marker || !legend || !lines || !lines.length) return;

    var visLines = Array.prototype.slice.call(svg.querySelectorAll(".traj-line"));
    var endDots = Array.prototype.slice.call(svg.querySelectorAll(".traj-end"));
    var halos = Array.prototype.slice.call(svg.querySelectorAll(".traj-halo"));
    var legItems = Array.prototype.slice.call(legend.querySelectorAll(".traj-leg"));
    var swatches = Array.prototype.slice.call(legend.querySelectorAll(".traj-swatch"));

    var hidden = {};   // line index -> true when toggled off
    var active = null; // highlighted line index, or null

    function byLine(list, i) {
      for (var k = 0; k < list.length; k++) {
        if (+list[k].dataset.line === i) return list[k];
      }
      return null;
    }

    function render() {
      for (var i = 0; i < lines.length; i++) {
        var isHidden = hidden[i] === true;
        var dim = isHidden || (active !== null && active !== i);
        var vis = byLine(visLines, i);
        var dot = byLine(endDots, i);
        var halo = byLine(halos, i);
        var leg = byLine(legItems, i);
        if (vis) {
          vis.style.display = isHidden ? "none" : "";
          vis.style.opacity = dim ? "0.12" : (active === i ? "1" : "0.9");
          vis.style.strokeWidth = active === i ? "3" : "1.75";
        }
        if (dot) {
          dot.style.display = isHidden ? "none" : "";
          dot.style.opacity = dim ? "0.12" : "1";
        }
        if (halo) halo.style.display = isHidden ? "none" : "";
        if (leg) leg.style.opacity = isHidden ? "0.4" : "1";
      }
    }

    function clearActive() {
      active = null;
      render();
      hide(tip); hide(marker);
    }

    function showTipFor(i, ev) {
      var ln = lines[i];
      if (!ln || hidden[i] === true || !ln.points || !ln.points.length) return;
      var p = nearestByX(ln.points, viewBoxX(svg, ev.clientX));
      if (!p) return;
      marker.setAttribute("cx", p.x);
      marker.setAttribute("cy", p.y);
      marker.setAttribute("fill", ln.color);
      show(marker);
      tip.textContent = ln.name + " \u00b7 #" + p.rank + " on " + p.label;
      placeTip(tip, wrap, svg, p.x, p.y);
      show(tip);
    }

    halos.forEach(function (h) {
      var i = +h.dataset.line;
      h.addEventListener("mouseenter", function () {
        if (hidden[i] === true) return;
        active = i; render();
      });
      h.addEventListener("mousemove", function (ev) { showTipFor(i, ev); });
      h.addEventListener("mouseleave", clearActive);
    });

    legItems.forEach(function (li) {
      var i = +li.dataset.line;
      li.addEventListener("mouseenter", function () {
        if (hidden[i] === true) return;
        active = i; render();
      });
      li.addEventListener("mouseleave", clearActive);
    });

    function toggle(i) {
      if (hidden[i] === true) { delete hidden[i]; }
      else { hidden[i] = true; if (active === i) active = null; }
      render();
    }
    swatches.forEach(function (sw) {
      var i = +sw.dataset.line;
      sw.addEventListener("click", function (ev) { ev.preventDefault(); toggle(i); });
      sw.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(i); }
      });
    });

    render();
  }

  ready(function () {
    initVolume();
    initTrajectory();
  });
})();
