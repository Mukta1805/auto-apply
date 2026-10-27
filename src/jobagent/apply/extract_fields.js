// Reads an application form and returns every field (label, type, required, options), plus CAPTCHA and
// submit-button info. Runs inside the page (Playwright page.evaluate). Read-only: it never changes the page.
// Verified against live Greenhouse (job-boards.greenhouse.io), Lever (jobs.lever.co) and Ashby forms.
(() => {
  const REQ = /[*✱]/g;
  const clean = (s) => (s || "").replace(REQ, " ").replace(/\s+/g, " ").trim();
  const hasReqMark = (s) => /[*✱]\s*$/.test((s || "").trim()) || /[*✱]/.test((s || "").slice(-4));
  const visible = (el) => {
    if (!el || el.type === "hidden") return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return el.type === "file"; // file inputs are often hidden
    return true;
  };
  const cssPath = (el) => {
    if (el.id && document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) return `#${CSS.escape(el.id)}`;
    if (el.name) {
      const sel = `${el.tagName.toLowerCase()}[name="${el.name.replace(/"/g, '\\"')}"]`;
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      let part = node.tagName.toLowerCase();
      const sib = Array.from(node.parentNode ? node.parentNode.children : []).filter((c) => c.tagName === node.tagName);
      if (sib.length > 1) part += `:nth-of-type(${sib.indexOf(node) + 1})`;
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  };
  // Text of a label element without the text of controls/options/buttons nested inside it.
  const ownText = (node) => {
    if (!node) return "";
    const c = node.cloneNode(true);
    c.querySelectorAll("input, select, textarea, option, button, ul, [role='listbox']").forEach((n) => n.remove());
    return c.innerText || c.textContent || "";
  };
  const textOf = (id) => { const n = id && document.getElementById(id); return n ? ownText(n) : ""; };
  // The question text for a control: the nearest question container's label that is not the control's own option label.
  const questionText = (el) => {
    const lever = el.closest(".application-question, li.application-question");
    if (lever) { const t = lever.querySelector(".application-label .text, .application-label"); if (t) return ownText(t); }
    const fs = el.closest("fieldset");
    if (fs && fs.querySelector("legend")) return ownText(fs.querySelector("legend"));
    // Walk up while the ancestor still holds only this control (or its radio/checkbox group); the innermost
    // label-like element found in that scope is the question. Stops before reaching a neighbouring field.
    const isOther = (c) => c !== el && !(el.name && c.name === el.name) && c.type !== "hidden";
    // Labels that belong to an option of this control's own radio/checkbox group are answers, not the question.
    const group = el.name ? Array.from(document.getElementsByName(el.name)) : [el];
    const groupIds = new Set(group.map((g) => g.id).filter(Boolean));
    const isOptionLabel = (lab) => (lab.htmlFor && groupIds.has(lab.htmlFor)) ||
      group.some((g) => lab.contains(g)) || !!lab.querySelector("input[type=radio], input[type=checkbox]");
    let node = el.parentElement;
    while (node && node !== document.body) {
      if (Array.from(node.querySelectorAll("input, select, textarea")).some(isOther)) break;
      for (const lab of node.querySelectorAll("legend, label, [class*='label' i], [id*='label' i], .text, h3, h4")) {
        if ((el.type === "radio" || el.type === "checkbox") && isOptionLabel(lab)) continue;
        if (/^(attach|enter manually|dropbox|google drive)$/i.test(clean(ownText(lab)))) continue;
        const t = ownText(lab);
        if (clean(t)) return t;
      }
      node = node.parentElement;
    }
    return "";
  };
  const labelFor = (el) => {
    let raw = "";
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) raw = ownText(l); }
    if (!raw && el.getAttribute("aria-labelledby")) raw = el.getAttribute("aria-labelledby").split(/\s+/).map(textOf).join(" ");
    if (!raw && el.getAttribute("aria-label")) raw = el.getAttribute("aria-label");
    if (!raw) { const wrap = el.closest("label"); if (wrap) raw = ownText(wrap); }
    if (!raw || /^(attach|type your response|select\.*)$/i.test(clean(raw))) raw = questionText(el) || raw;
    if (!raw && el.placeholder) raw = el.placeholder;
    return raw;
  };
  const isRequired = (el, raw) => el.required || el.getAttribute("aria-required") === "true" || hasReqMark(raw) ||
    !!(el.closest(".application-question") && /[*✱]/.test((el.closest(".application-question").querySelector(".application-label") || {}).innerText || ""));

  const fields = [];
  const groups = {};
  for (const el of document.querySelectorAll("input, select, textarea, [role='combobox']")) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || (tag === "select" ? "select" : tag === "textarea" ? "textarea" : "text")).toLowerCase();
    if (["hidden", "submit", "button", "image", "reset", "search"].includes(type)) continue;
    if (!visible(el)) continue;
    if (el.closest("[aria-hidden='true']") && type !== "file") continue;
    if (type === "radio" || type === "checkbox") {
      const gname = el.name || cssPath(el);
      const own = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const optLabel = clean(own ? ownText(own) : ownText(el.closest("label")) || el.value);
      if (!groups[gname]) {
        const q = questionText(el);
        groups[gname] = { selector: el.name ? `input[name="${el.name.replace(/"/g, '\\"')}"]` : cssPath(el), tag: "input", type,
                          name: el.name || "", id: el.id || "", label: clean(q).slice(0, 400), required: false, options: [] };
        fields.push(groups[gname]);
      }
      groups[gname].options.push({ value: el.value, text: optLabel.slice(0, 200), selector: cssPath(el) });
      groups[gname].required = groups[gname].required || el.required || hasReqMark(questionText(el));
      continue;
    }
    const raw = labelFor(el);
    const f = { selector: cssPath(el), tag, type: el.getAttribute("role") === "combobox" && tag !== "select" ? "combobox" : type,
                name: el.name || "", id: el.id || "", label: clean(raw).slice(0, 400), required: isRequired(el, raw),
                placeholder: el.placeholder || "", maxlength: el.maxLength > 0 ? el.maxLength : null };
    if (tag === "select") f.options = Array.from(el.options).map((o) => ({ value: o.value, text: clean(o.text) })).filter((o) => o.text);
    if (type === "file") {
      const q = questionText(el);
      f.accept = el.accept || "";
      f.label = clean(q || raw).slice(0, 400) || f.label;
      f.required = el.required || el.getAttribute("aria-required") === "true" || hasReqMark(q);
    }
    fields.push(f);
  }

  const captcha = [];
  document.querySelectorAll("iframe").forEach((f) => {
    const src = (f.src || "").toLowerCase();
    if (/recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha/.test(src)) {
      const r = f.getBoundingClientRect();
      captcha.push({ src: src.split(/[?#]/)[0], invisible: /size=invisible/.test(src) || r.width < 5 || r.height < 5 });
    }
  });
  const invisibleCaptcha = !!document.querySelector(".grecaptcha-badge, #hcaptchaSubmitBtn, [data-size='invisible']");
  const submit = Array.from(document.querySelectorAll("button, input[type=submit]"))
    .filter((b) => /submit|send application/i.test(b.innerText || b.value || "") && visible(b))
    .map((b) => ({ selector: cssPath(b), text: clean(b.innerText || b.value) }));
  return { url: location.href, title: document.title, fields, captcha, invisibleCaptcha, submit };
})()
