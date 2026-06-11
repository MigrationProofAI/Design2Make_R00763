/* d2m_render.js -- deterministic, model-free renderers for D2M structured results.
   Implements the skills/d2m-render/SKILL.md contract. The model emits structured data;
   these pure functions draw it. Self-contained: injects its own (namespaced d2m-*) styles
   and attaches window.d2mRender(tool, payload, el) -> bool (true if it rendered a typed card).
   Today: MRP (run_mrp). Add siblings (material/BOM/cost/PIR/routing) as pure functions here. */
(function () {
  "use strict";

  // ---- styles (injected once; namespaced so they never collide with the app/data panel) ----
  if (!document.getElementById("d2m-render-css")) {
    const css =
      ".d2m-banner{border-radius:9px;padding:8px 12px;font-size:12.5px;font-weight:600;margin-bottom:10px;border:1px solid}" +
      ".d2m-banner.ok{background:#ecfdf3;color:#16a34a;border-color:#bbf7d0}" +
      ".d2m-banner.err{background:#fef2f2;color:#dc2626;border-color:#fecaca}" +
      ".d2m-banner.empty{background:#f1f5f9;color:#6b7785;border-color:#e4e8ef}" +
      ".d2m-slab{background:#fff;border:1px solid #e4e8ef;border-radius:10px;padding:11px 13px;margin-bottom:10px}" +
      ".d2m-slab h2{margin:0 0 7px;font-size:14px}" +
      ".d2m-meta{display:flex;flex-wrap:wrap;gap:4px 14px}.d2m-meta span{font-size:11.5px;color:#6b7785}.d2m-meta b{color:#1f2733;font-weight:600}" +
      ".d2m-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));gap:8px;margin-bottom:11px}" +
      ".d2m-tile{background:#fff;border:1px solid #e4e8ef;border-radius:9px;padding:9px 6px;text-align:center}" +
      ".d2m-tile .n{font-size:20px;font-weight:700;line-height:1}.d2m-tile .l{font-size:10px;color:#6b7785;margin-top:4px;text-transform:uppercase;letter-spacing:.03em}" +
      ".d2m-tile.blue .n{color:#2563eb}.d2m-tile.amber .n{color:#d97706}.d2m-tile.green .n{color:#16a34a}.d2m-tile.red .n{color:#dc2626}.d2m-tile.muted .n{color:#6b7785}" +
      ".d2m-sec{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#6b7785;margin:2px 2px 7px}" +
      ".d2m-tree{background:#fff;border:1px solid #e4e8ef;border-radius:10px;overflow:hidden}" +
      ".d2m-trow{display:flex;align-items:center;gap:8px;padding:7px 11px;border-bottom:1px solid #e4e8ef;font-size:12.5px}.d2m-trow:last-child{border-bottom:0}.d2m-trow.exc{background:#fffbeb}" +
      ".d2m-lvl{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#6b7785;width:18px}" +
      ".d2m-mat{font-family:ui-monospace,Consolas,monospace;font-weight:700}.d2m-lbl{color:#6b7785;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      ".d2m-out{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:999px}.d2m-out.blue{background:#eff4ff;color:#2563eb}.d2m-out.amber{background:#fff7ed;color:#d97706}" +
      ".d2m-badge{font-size:9.5px;font-weight:700;padding:2px 6px;border-radius:5px}.d2m-badge.made{background:#eff4ff;color:#2563eb}.d2m-badge.bought{background:#fff7ed;color:#d97706}" +
      ".d2m-excmark{font-size:10.5px;font-weight:700;color:#d97706}" +
      ".d2m-excfoot{margin-top:10px;background:#fffbeb;border:1px solid #fde68a;border-radius:9px;padding:9px 12px;font-size:11.5px;color:#92400e}.d2m-excfoot b{color:#78350f}" +
      ".d2m-kv{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:10px;background:#fff;border:1px solid #e4e8ef;border-radius:10px;overflow:hidden}" +
      ".d2m-kv td{padding:6px 11px;border-bottom:1px solid #eef2f7;vertical-align:top}.d2m-kv tr:last-child td{border-bottom:0}" +
      ".d2m-kv td.dk{color:#6b7785;font-weight:600;white-space:nowrap;width:42%}";
    const st = document.createElement("style"); st.id = "d2m-render-css"; st.textContent = css;
    document.head.appendChild(st);
  }

  // ---- shared primitives ----
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const _int = (v) => { const n = parseInt(v, 10); return isNaN(n) ? 0 : n; };

  function _banner(kind, text) { const b = document.createElement("div"); b.className = "d2m-banner " + kind; b.textContent = text; return b; }
  function _slab(title, meta) {
    const s = document.createElement("div"); s.className = "d2m-slab";
    const h = document.createElement("h2"); h.textContent = title; s.appendChild(h);
    const m = document.createElement("div"); m.className = "d2m-meta";
    meta.filter(([, v]) => v != null && v !== "").forEach(([k, v]) => {
      const sp = document.createElement("span"); sp.innerHTML = esc(k) + ": <b>" + esc(v) + "</b>"; m.appendChild(sp);
    });
    s.appendChild(m); return s;
  }
  function _tiles(items) {
    const wrap = document.createElement("div"); wrap.className = "d2m-tiles";
    items.forEach(([label, n, tone]) => {
      const t = document.createElement("div"); t.className = "d2m-tile " + (tone || "muted");
      t.innerHTML = '<div class="n">' + _int(n) + '</div><div class="l">' + esc(label) + '</div>';
      wrap.appendChild(t);
    });
    return wrap;
  }
  function _badge(type) {
    const made = ["FERT", "HALB"].includes(String(type).toUpperCase());
    return '<span class="d2m-badge ' + (made ? "made" : "bought") + '">' + esc(type) + "</span>";
  }
  function _sec(text) { const d = document.createElement("div"); d.className = "d2m-sec"; d.textContent = text; return d; }
  function _cascadeRow(m) {
    const row = document.createElement("div"); row.className = "d2m-trow" + (m.exception ? " exc" : "");
    row.style.paddingLeft = (11 + _int(m.level) * 20) + "px";
    const out = m.output || (String(m.proc).toUpperCase() === "F" ? "purchase_req" : "planned_order");
    const tone = out === "planned_order" ? "blue" : "amber";
    row.innerHTML =
      '<span class="d2m-lvl">L' + _int(m.level) + "</span>" + _badge(m.type) +
      '<span class="d2m-mat">' + esc(m.mat) + "</span>" +
      '<span class="d2m-lbl">' + esc(m.label || "") + "</span>" +
      '<span class="d2m-out ' + tone + '">' + esc(out.replace("_", " ")) + "</span>" +
      (m.exception ? '<span class="d2m-excmark" title="MRP exception message">! exception</span>' : "");
    return row;
  }

  // ---- MRP / planning (exact, proven contract) ----
  function renderMrpResult(data, el) {
    el.innerHTML = "";
    if (!data) { el.appendChild(_banner("empty", "No MRP result to show.")); return; }
    const run = data.run || {};
    const mats = Array.isArray(data.materials) ? data.materials : [];
    const failed = ["E", "A"].includes(String(data.status || "").toUpperCase()) || _int(run.errors) > 0;

    el.appendChild(_banner(failed ? "err" : "ok",
      data.message || (failed ? "MRP run reported errors." : "MRP carried out.")));
    el.appendChild(_slab("MRP · " + (data.material || "?"), [
      ["Plant", data.plant],
      ["Mode", data.multiLevel ? "Multi-level (BOM)" : "Single-level"],
      ["Status", data.status],
      ["Run", data.timestamp],
    ]));
    el.appendChild(_tiles([
      ["Planned orders", run.plannedOrdersCreated, "blue"],
      ["Purchase reqs", run.purchaseReqsCreated, "amber"],
      ["Orders deleted", run.plannedOrdersDeleted, "muted"],
      ["Errors", run.errors, _int(run.errors) > 0 ? "red" : "green"],
    ]));
    if (!mats.length) {
      el.appendChild(_banner("empty", "No MRP elements were planned (no demand, or nothing to cover)."));
    } else {
      el.appendChild(_sec("BOM cascade · " + mats.length + " materials"));
      const tree = document.createElement("div"); tree.className = "d2m-tree";
      mats.forEach((m) => tree.appendChild(_cascadeRow(m)));
      el.appendChild(tree);
    }
    const exc = mats.filter((m) => m.exception);
    if (exc.length) {
      const f = document.createElement("div"); f.className = "d2m-excfoot";
      f.innerHTML = "<b>" + exc.length + " material(s) carry an MRP exception message</b> — " +
        exc.map((m) => esc(m.mat)).join(", ") + ". Review coverage/dates (e.g. MD04).";
      el.appendChild(f);
    }
  }

  // ---- Material (from get_material / explore_entity on A_Product; raw OData shape) ----
  function _firstRow(payload) {                       // unwrap {d:{results:[..]}} or {d:{..}} or {..}
    const d = payload && payload.d ? payload.d : payload;
    if (!d) return null;
    return Array.isArray(d.results) ? (d.results[0] || null) : d;
  }
  function _desc(row) {
    const dd = row.to_Description && row.to_Description.results;
    if (Array.isArray(dd) && dd.length) {
      const en = dd.find((x) => String(x.Language).toUpperCase() === "EN") || dd[0];
      return en.ProductDescription || "";
    }
    return row.ProductDescription || "";
  }
  function _plants(row) {
    const pp = row.to_Plant && row.to_Plant.results;
    return Array.isArray(pp) ? pp.map((p) => p.Plant).filter(Boolean) : [];
  }
  function _kv(pairs) {
    const t = document.createElement("table"); t.className = "d2m-kv";
    pairs.filter(([, v]) => v != null && v !== "").forEach(([k, v]) => {
      const r = t.insertRow(); const a = r.insertCell(); a.className = "dk"; a.textContent = k;
      const b = r.insertCell(); b.textContent = String(v);
    });
    return t;
  }

  function renderMaterial(payload, el) {
    el.innerHTML = "";
    const row = _firstRow(payload);
    if (!row || row.Product == null) { el.appendChild(_banner("empty", "Material not found.")); return; }
    el.appendChild(_banner("ok", "Material " + esc(row.Product)));
    const slab = document.createElement("div"); slab.className = "d2m-slab";
    slab.innerHTML = '<h2>Material · ' + esc(row.Product) + "  " + _badge(row.ProductType) + "</h2>";
    const m = document.createElement("div"); m.className = "d2m-meta";
    [["Base unit", row.BaseUnit], ["Group", row.ProductGroup], ["Industry", row.IndustrySector],
     ["Status", row.CrossPlantStatus]].filter(([, v]) => v != null && v !== "").forEach(([k, v]) => {
      const sp = document.createElement("span"); sp.innerHTML = esc(k) + ": <b>" + esc(v) + "</b>"; m.appendChild(sp);
    });
    slab.appendChild(m); el.appendChild(slab);
    el.appendChild(_kv([
      ["Description", _desc(row)],
      ["Gross weight", row.GrossWeight && row.GrossWeight + " " + (row.WeightUnit || "")],
      ["Net weight", row.NetWeight && row.NetWeight + " " + (row.WeightUnit || "")],
      ["Division", row.Division],
    ]));
    const plants = _plants(row);
    if (plants.length) {
      el.appendChild(_sec("Plants · " + plants.length));
      const tree = document.createElement("div"); tree.className = "d2m-tree";
      plants.forEach((p) => {
        const r = document.createElement("div"); r.className = "d2m-trow";
        r.innerHTML = '<span class="d2m-mat">' + esc(p) + '</span><span class="d2m-lbl">plant view</span>';
        tree.appendChild(r);
      });
      el.appendChild(tree);
    }
  }

  // ---- assurance / boardroom grounding: findings + BOM composition + country of origin ----
  function renderAssurance(payload, el) {
    const p = payload || {}, sum = p.summary || {};
    const comps = (p.facts && p.facts.components) || [];
    const findings = (p.findings || []).filter((f) => f.verdict !== "pass");
    const errs = _int(sum.error), warns = _int(sum.warning), revs = _int(sum.review), total = _int(sum.total_findings);
    el.appendChild(_banner(errs ? "err" : (total ? "empty" : "ok"),
      errs ? (errs + " error" + (errs === 1 ? "" : "s") + " — escalate to human")
           : (total ? (total + " finding" + (total === 1 ? "" : "s")) : "assurance clean — no findings")));
    el.appendChild(_slab("🛡 Assurance · " + esc(p.material || ""), [
      ["plant", p.plant], ["components", comps.length], ["source", (p.facts && p.facts.bom_source) || ""]]));
    el.appendChild(_tiles([["errors", errs, "red"], ["warnings", warns, "amber"], ["reviews", revs, "blue"], ["findings", total, "muted"]]));
    if (comps.length) {
      const s = document.createElement("div"); s.className = "d2m-sec"; s.textContent = "BOM composition"; el.appendChild(s);
      const tree = document.createElement("div"); tree.className = "d2m-tree";
      comps.forEach((c) => {
        const bought = /HAWA|ROH|VERP/.test(c.ProductType || "");
        const qty = c.bom_quantity != null ? (" ×" + c.bom_quantity) : "";
        const r = document.createElement("div"); r.className = "d2m-trow";
        r.innerHTML = '<span class="d2m-mat">' + esc(c.Product) + "</span>"
          + '<span class="d2m-lbl">' + esc(c.description || "") + esc(qty) + "</span>"
          + '<span class="d2m-badge ' + (bought ? "bought" : "made") + '">' + esc(c.ProductType || "") + "</span>"
          + '<span class="d2m-lbl" style="flex:0;min-width:30px;text-align:right">' + (c.CountryOfOrigin ? esc(c.CountryOfOrigin) : "—") + "</span>";
        tree.appendChild(r);
      });
      el.appendChild(tree);
    }
    if (findings.length) {
      const s = document.createElement("div"); s.className = "d2m-sec"; s.textContent = "Findings"; el.appendChild(s);
      const tree = document.createElement("div"); tree.className = "d2m-tree";
      findings.slice(0, 40).forEach((f) => {
        const r = document.createElement("div"); r.className = "d2m-trow" + (f.severity === "error" ? " exc" : "");
        r.innerHTML = '<span class="d2m-out amber">' + esc(f.severity || "") + "</span>"
          + '<span class="d2m-mat">' + esc(f.object || "") + "</span>"
          + '<span class="d2m-lbl">' + esc(f.fact || "") + "</span>"
          + '<span class="d2m-lbl" style="flex:0;color:#94a3b8">' + esc(f.against || "") + "</span>";
        tree.appendChild(r);
      });
      el.appendChild(tree);
    }
  }

  // ---- dispatcher: pick the typed renderer by producing tool; false => caller falls back ----
  function d2mRender(tool, payload, el) {
    if (tool === "assure_assembly" && payload && payload.facts) { renderAssurance(payload, el); return true; }
    if (tool === "run_mrp" && payload && (payload.run || payload.materials)) {
      renderMrpResult(payload, el); return true;
    }
    if (tool === "get_material") {
      const r = _firstRow(payload); if (r && r.Product != null) { renderMaterial(payload, el); return true; }
    }
    if (tool === "explore_entity") {                  // only when the row IS a material (A_Product)
      const r = _firstRow(payload);
      if (r && r.Product != null && r.ProductType != null) { renderMaterial(payload, el); return true; }
    }
    return false;
  }

  window.renderMrpResult = renderMrpResult;
  window.renderMaterial = renderMaterial;
  window.renderAssurance = renderAssurance;
  window.d2mRender = d2mRender;
})();
