const speakerIds = Array.from({ length: 12 }, (_, i) => "S" + (i + 1));
const pageSize = 20;

export function speakerLabel(id, names) {
  if (!speakerIds.includes(id)) return "話者未設定";
  const name = names?.[id];
  return (typeof name === "string" && name.trim()) || "話者" + id.slice(1);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function timestamp(seconds) {
  const t = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = n => String(n).padStart(2, "0");
  return (t >= 3600 ? Math.floor(t / 3600) + ":" : "") + pad(Math.floor(t / 60) % 60) + ":" + pad(t % 60);
}

export function speakerEditor(segments, names, onSave) {
  const open = element("button", "話者を編集", "speaker-open");
  open.type = "button";
  open.setAttribute("aria-haspopup", "dialog");
  open.disabled = !segments.length;
  open.onclick = () => showEditor(open, segments, names, onSave);
  return open;
}

function showEditor(open, segments, names, onSave) {
  // 変更は保存されるまで会議データに反映しない。
  const speakers = segments.map(segment => speakerIds.includes(segment.speaker) ? segment.speaker : "");
  const activeIds = new Set(speakers.filter(Boolean));
  const draftNames = {};
  for (const id of speakerIds) {
    if (typeof names?.[id] === "string") {
      activeIds.add(id);
      draftNames[id] = names[id];
    }
  }
  if (!activeIds.size) activeIds.add("S1");
  const dialog = element("dialog", "", "speaker-dialog");
  const title = element("h2", "話者を編集");
  dialog.setAttribute("aria-label", "話者を編集");
  const fields = element("fieldset");
  const nameFields = element("div", "", "speaker-names");
  const add = element("button", "＋ 話者を追加");
  const rows = element("div", "", "speaker-rows");
  const pageLabel = element("span");
  pageLabel.setAttribute("role", "status");
  const previous = element("button", "前へ");
  const next = element("button", "次へ");
  const paging = element("div", "", "speaker-pages");
  paging.append(previous, pageLabel, next);
  const error = element("p", "", "speaker-error");
  error.setAttribute("role", "alert");
  const buttons = element("div", "", "speaker-actions");
  const cancel = element("button", "キャンセル");
  const save = element("button", "保存", "primary");
  buttons.append(cancel, save);
  fields.append(element("h3", "話者名"), nameFields, add, element("h3", "発言ごとの話者"), paging, rows);
  dialog.append(title, fields, error, buttons);

  let page = 0;
  let saving = false;
  function optionLabel(id) {
    const fallback = speakerLabel(id, {});
    const label = speakerLabel(id, draftNames);
    return label === fallback ? fallback : label + "（" + fallback + "）";
  }
  function addNameField(id) {
    const label = element("label", speakerLabel(id, {}));
    const input = element("input");
    input.type = "text";
    input.maxLength = 60;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "名前を入力";
    input.value = draftNames[id] || "";
    input.oninput = () => {
      draftNames[id] = input.value;
      for (const option of rows.querySelectorAll("option")) {
        if (option.value === id) option.textContent = optionLabel(id);
      }
    };
    label.append(input);
    nameFields.append(label);
    return input;
  }
  function renderPage() {
    const first = page * pageSize;
    const last = Math.min(first + pageSize, segments.length);
    previous.disabled = page === 0;
    next.disabled = last === segments.length;
    pageLabel.textContent = (first + 1) + "–" + last + " / " + segments.length + " 件";
    rows.replaceChildren();
    for (let index = first; index < last; index++) {
      const segment = segments[index];
      const time = timestamp(segment.start);
      const row = element("div", "", "speaker-row");
      const select = element("select");
      select.setAttribute("aria-label", (index + 1) + "番目の発言（" + time + "）の話者");
      select.add(new Option("話者未設定", ""));
      for (const id of speakerIds) {
        if (activeIds.has(id)) select.add(new Option(optionLabel(id), id));
      }
      select.value = speakers[index];
      select.onchange = () => { speakers[index] = select.value; };
      row.append(element("time", time), element("p", segment.text), select);
      rows.append(row);
    }
  }
  for (const id of speakerIds) {
    if (activeIds.has(id)) addNameField(id);
  }
  add.disabled = activeIds.size === speakerIds.length;
  add.onclick = () => {
    const id = speakerIds.find(candidate => !activeIds.has(candidate));
    if (!id) return;
    activeIds.add(id);
    addNameField(id).focus();
    add.disabled = activeIds.size === speakerIds.length;
    renderPage();
  };
  previous.onclick = () => { page--; renderPage(); };
  next.onclick = () => { page++; renderPage(); };
  cancel.onclick = () => dialog.close();
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  dialog.onclose = () => {
    dialog.remove();
    if (open.isConnected) open.focus();
  };
  save.onclick = async () => {
    if (saving) return;
    saving = fields.disabled = cancel.disabled = save.disabled = true;
    save.textContent = "保存中…";
    error.textContent = "";
    try {
      const cleanedNames = {};
      for (const id of activeIds) {
        const name = draftNames[id]?.trim();
        if (name) cleanedNames[id] = name;
      }
      await onSave({ names: cleanedNames, speakers });
      dialog.close();
    } catch (reason) {
      error.textContent = "話者の変更を保存できませんでした。" + (reason?.message || "");
    } finally {
      saving = fields.disabled = cancel.disabled = save.disabled = false;
      save.textContent = "保存";
    }
  };
  renderPage();
  document.body.append(dialog);
  dialog.showModal();
}
