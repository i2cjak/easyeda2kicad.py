const appState = {
  payload: null,
  library: { version: 1, library: "easyeda2kicad", folders: [], symbols: [] },
  activeFolderId: null,
  activeSymbolId: null,
  codexApplied: false,
  codexRunning: false,
  activeCodexJobId: null,
  validation: null,
  payloadRevision: 0,
  validationRevision: -1,
  previewZoom: 1,
  previewPanX: 0,
  previewPanY: 0,
  previewBounds: { x: -220, y: -170, width: 440, height: 340 },
  previewDrag: null,
};

const STORAGE_KEY = "jlc-kicad-symbol-editor-library-v1";

const form = document.getElementById("importForm");
const lcscInput = document.getElementById("lcscInput");
const codexPass = document.getElementById("codexPass");
const folderTabs = document.getElementById("folderTabs");
const librarySummary = document.getElementById("librarySummary");
const clearLibraryButton = document.getElementById("clearLibraryButton");
const statusPill = document.getElementById("statusPill");
const codexState = document.getElementById("codexState");
const overview = document.getElementById("overview");
const notes = document.getElementById("notes");
const pinTableBody = document.querySelector("#pinTable tbody");
const propertyForm = document.getElementById("propertyForm");
const customFields = document.getElementById("customFields");
const cleanupButton = document.getElementById("cleanupButton");
const validateButton = document.getElementById("validateButton");
const addFieldButton = document.getElementById("addFieldButton");
const runCodexButton = document.getElementById("runCodexButton");
const downloadSymbolButton = document.getElementById("downloadSymbolButton");
const downloadBundleButton = document.getElementById("downloadBundleButton");
const symbolSelect = document.getElementById("symbolSelect");
const unitSelect = document.getElementById("unitSelect");
const zoomOutButton = document.getElementById("zoomOutButton");
const zoomResetButton = document.getElementById("zoomResetButton");
const zoomInButton = document.getElementById("zoomInButton");
const panLeftButton = document.getElementById("panLeftButton");
const panUpButton = document.getElementById("panUpButton");
const panDownButton = document.getElementById("panDownButton");
const panRightButton = document.getElementById("panRightButton");
const themeToggle = document.getElementById("themeToggle");
const preview = document.getElementById("symbolPreview");
const kicadPreviewShell = document.getElementById("kicadPreviewShell");
const kicadPreview = document.getElementById("kicadPreview");
const footprintPreviewShell = document.getElementById("footprintPreviewShell");
const footprintPreview = document.getElementById("footprintPreview");
themeToggle.textContent = document.body.classList.contains("dark") ? "Light" : "Dark";

const propertyFields = [
  ["name", "Symbol"],
  ["prefix", "Reference"],
  ["package", "Footprint"],
  ["fp_filters", "Footprint Filters"],
  ["manufacturer", "Manufacturer"],
  ["mpn", "MPN"],
  ["datasheet", "Datasheet"],
  ["lcsc_id", "LCSC Part"],
  ["keywords", "Keywords"],
  ["description", "Description"],
];
let customFieldCounter = 0;

function setStatus(text, isError = false) {
  statusPill.textContent = text;
  statusPill.classList.toggle("error", isError);
}

function showError(message) {
  notes.innerHTML = "";
  const note = document.createElement("p");
  note.className = "note error";
  note.textContent = message;
  notes.append(note);
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function escapeText(value) {
  return String(value ?? "");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeId(prefix) {
  if (window.crypto?.randomUUID) {
    return `${prefix}-${window.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeLibrary(raw) {
  const library = raw && typeof raw === "object" ? raw : {};
  return {
    version: 1,
    library: String(library.library || "easyeda2kicad"),
    folders: Array.isArray(library.folders) ? library.folders : [],
    symbols: Array.isArray(library.symbols) ? library.symbols : [],
  };
}

function restoreSession() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return;
  try {
    const data = JSON.parse(raw);
    appState.library = normalizeLibrary(data.library);
    appState.activeFolderId = data.activeFolderId || appState.library.folders[0]?.id || null;
    appState.activeSymbolId = data.activeSymbolId || null;
    for (const item of appState.library.symbols) {
      scrubCodexState(item);
    }
    const active = symbolItemById(appState.activeSymbolId) || firstSymbolInFolder(appState.activeFolderId) || appState.library.symbols[0] || null;
    if (active) {
      appState.activeFolderId = active.folderId;
      loadSymbolState(active);
    }
  } catch (error) {
    localStorage.removeItem(STORAGE_KEY);
  }
}

function scrubCodexState(item) {
  item.codexApplied = false;
  item.codexRunning = false;
  item.activeCodexJobId = null;
  if (item.payload) {
    delete item.payload.codex_job_id;
  }
}

let persistTimer = null;
function queuePersistSession() {
  if (persistTimer !== null) return;
  persistTimer = window.setTimeout(() => {
    persistTimer = null;
    persistSession();
  }, 0);
}

function persistSession() {
  syncActiveUnit();
  saveActiveSymbolState();
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    library: appState.library,
    activeFolderId: appState.activeFolderId,
    activeSymbolId: appState.activeSymbolId,
  }));
}

function symbolItemById(id) {
  return appState.library.symbols.find((item) => item.id === id) || null;
}

function symbolsForActiveFolder() {
  if (!appState.activeFolderId) return appState.library.symbols;
  return appState.library.symbols.filter((item) => item.folderId === appState.activeFolderId);
}

function firstSymbolInFolder(folderId) {
  return appState.library.symbols.find((item) => item.folderId === folderId) || null;
}

function activeSymbolItem() {
  return symbolItemById(appState.activeSymbolId);
}

function saveActiveSymbolState() {
  const item = activeSymbolItem();
  if (!item) return;
  syncActiveUnit();
  item.payload = appState.payload;
  item.validation = appState.validation;
  item.payloadRevision = appState.payloadRevision;
  item.validationRevision = appState.validationRevision;
  item.previewZoom = appState.previewZoom;
  item.previewPanX = appState.previewPanX;
  item.previewPanY = appState.previewPanY;
  item.codexApplied = appState.codexApplied;
  item.codexRunning = appState.codexRunning;
  item.activeCodexJobId = appState.activeCodexJobId;
}

function loadSymbolState(item) {
  appState.activeSymbolId = item?.id || null;
  appState.activeFolderId = item?.folderId || appState.activeFolderId;
  appState.payload = item?.payload || null;
  appState.validation = item?.validation || null;
  appState.payloadRevision = Number(item?.payloadRevision || 0);
  appState.validationRevision = Number(item?.validationRevision ?? -1);
  appState.previewZoom = Number(item?.previewZoom || 1);
  appState.previewPanX = Number(item?.previewPanX || 0);
  appState.previewPanY = Number(item?.previewPanY || 0);
  appState.codexApplied = Boolean(item?.codexApplied);
  appState.codexRunning = Boolean(item?.codexRunning);
  appState.activeCodexJobId = item?.activeCodexJobId || null;
  loadActiveUnitPins();
}

function setActiveSymbol(symbolId) {
  if (symbolId === appState.activeSymbolId) return;
  saveActiveSymbolState();
  const item = symbolItemById(symbolId);
  if (item) {
    loadSymbolState(item);
  } else {
    appState.activeSymbolId = null;
    appState.payload = null;
    appState.validation = null;
    appState.validationRevision = -1;
  }
  renderAll();
  queuePersistSession();
}

function activeUnitIndex() {
  const units = appState.payload?.symbol?.units;
  if (!Array.isArray(units) || !units.length) return 0;
  const raw = Number(appState.payload.symbol.active_unit || 0);
  return Math.max(0, Math.min(units.length - 1, Number.isFinite(raw) ? raw : 0));
}

function syncActiveUnit() {
  const symbol = appState.payload?.symbol;
  const units = symbol?.units;
  if (!Array.isArray(units) || !units.length) return;
  units[activeUnitIndex()].pins = clone(symbol.pins || []);
}

function loadActiveUnitPins() {
  const symbol = appState.payload?.symbol;
  const units = symbol?.units;
  if (!Array.isArray(units) || !units.length) return;
  const index = activeUnitIndex();
  symbol.active_unit = index;
  symbol.pins = clone(units[index].pins || []);
}

function switchActiveUnit(index) {
  if (!appState.payload) return;
  syncActiveUnit();
  appState.payload.symbol.active_unit = index;
  loadActiveUnitPins();
  resetPreviewZoom();
  renderAll();
  queuePersistSession();
}

function createImportFolder(ids) {
  const folder = {
    id: makeId("folder"),
    name: ids.length === 1 ? ids[0] : `${ids[0]} +${ids.length - 1}`,
    createdAt: new Date().toISOString(),
  };
  appState.library.folders.push(folder);
  appState.activeFolderId = folder.id;
  return folder;
}

function addPayloadToLibrary(payload, folderId) {
  saveActiveSymbolState();
  const info = payload?.symbol?.info || {};
  const item = {
    id: makeId("symbol"),
    folderId,
    payload,
    validation: null,
    payloadRevision: 0,
    validationRevision: -1,
    previewZoom: 1,
    previewPanX: 0,
    previewPanY: 0,
    codexApplied: false,
    codexRunning: Boolean(payload?.codex_job_id),
    activeCodexJobId: payload?.codex_job_id || null,
    label: info.name || payload?.lcsc_id || "Imported symbol",
  };
  appState.library.symbols.push(item);
  loadSymbolState(item);
  return item;
}

function parseImportIds(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
}

function libraryExportPayload() {
  persistSession();
  return {
    library: appState.library.library || "easyeda2kicad",
    folders: appState.library.folders,
    symbols: appState.library.symbols.map((item) => ({
      id: item.id,
      folder_id: item.folderId,
      payload: item.payload,
    })),
  };
}

function anyCodexRunning() {
  return appState.library.symbols.some((item) => item.codexRunning);
}

function cancelCodexJobs() {
  for (const item of appState.library.symbols) {
    scrubCodexState(item);
  }
  appState.codexApplied = false;
  appState.codexRunning = false;
  appState.activeCodexJobId = null;
  queuePersistSession();
  fetch("/api/codex/cancel_all", { method: "POST", keepalive: true }).catch(() => {});
}

function renderAll() {
  updateActionState();
  const payload = appState.payload;
  const active = Boolean(payload);
  if (!active) {
    customFieldCounter = 0;
  }
  renderOverview();
  renderLibrary();
  renderNotes();
  renderProperties();
  renderCustomFields();
  renderPins();
  renderPreview();
  renderKicadPreview();
  renderFootprintPreview();
}

function updateActionState() {
  const active = Boolean(appState.payload);
  const hasSymbols = appState.library.symbols.length > 0;
  const running = anyCodexRunning();
  cleanupButton.disabled = !active;
  validateButton.disabled = !active || appState.codexRunning;
  addFieldButton.disabled = !active;
  runCodexButton.disabled = !active || appState.codexRunning;
  const canDownload = hasSymbols && !running;
  downloadSymbolButton.disabled = !canDownload;
  downloadBundleButton.disabled = !canDownload;
  const title = canDownload ? "Export validates the loaded library before downloading" : "Import at least one symbol and wait for Codex to finish";
  downloadSymbolButton.title = title;
  downloadBundleButton.title = title;
  clearLibraryButton.disabled = !hasSymbols;
  symbolSelect.disabled = !hasSymbols;
  zoomOutButton.disabled = !active;
  zoomResetButton.disabled = !active;
  zoomInButton.disabled = !active;
  panLeftButton.disabled = !active;
  panUpButton.disabled = !active;
  panDownButton.disabled = !active;
  panRightButton.disabled = !active;
}

function renderLibrary() {
  folderTabs.innerHTML = "";
  for (const folder of appState.library.folders) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost folder-tab";
    button.classList.toggle("is-active", folder.id === appState.activeFolderId);
    const count = appState.library.symbols.filter((item) => item.folderId === folder.id).length;
    button.textContent = `${folder.name} (${count})`;
    button.title = folder.name;
    button.addEventListener("click", () => {
      saveActiveSymbolState();
      appState.activeFolderId = folder.id;
      const current = activeSymbolItem();
      if (!current || current.folderId !== folder.id) {
        const first = firstSymbolInFolder(folder.id);
        if (first) loadSymbolState(first);
      }
      renderAll();
      queuePersistSession();
    });
    folderTabs.append(button);
  }

  symbolSelect.innerHTML = "";
  const symbols = symbolsForActiveFolder();
  if (!symbols.length) {
    const option = document.createElement("option");
    option.textContent = "No symbols";
    symbolSelect.append(option);
  }
  for (const item of symbols) {
    const option = document.createElement("option");
    const info = item.payload?.symbol?.info || {};
    option.value = item.id;
    option.textContent = info.name || item.payload?.lcsc_id || item.label || "Imported symbol";
    option.selected = item.id === appState.activeSymbolId;
    symbolSelect.append(option);
  }

  unitSelect.innerHTML = "";
  const units = appState.payload?.symbol?.units;
  if (Array.isArray(units) && units.length) {
    units.forEach((unit, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = unit.name || `Unit ${index + 1}`;
      option.selected = index === activeUnitIndex();
      unitSelect.append(option);
    });
    unitSelect.disabled = units.length <= 1;
  } else {
    const option = document.createElement("option");
    option.value = "0";
    option.textContent = "Unit 1";
    unitSelect.append(option);
    unitSelect.disabled = true;
  }

  const folderCount = appState.library.folders.length;
  const symbolCount = appState.library.symbols.length;
  librarySummary.textContent = symbolCount
    ? `${symbolCount} symbols / ${folderCount} folders`
    : "No symbols";
}

function hasCurrentValidation() {
  return Boolean(
    appState.validation
    && appState.validationRevision === appState.payloadRevision
    && appState.validation.status !== "error"
  );
}

function renderOverview() {
  overview.innerHTML = "";
  if (!appState.payload) {
    return;
  }
  const appendRow = (key, value) => {
    if (value === undefined || value === null || value === "") return;
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value;
    overview.append(dt, dd);
  };
  const data = appState.payload.overview || {};
  const rows = [
    ["LCSC", data.lcsc_id],
    ["MPN", data.mpn],
    ["Manufacturer", data.manufacturer],
    ["Package", data.package],
    ["Stock", data.stock],
    ["Library", data.library_type],
    ["Datasheet", data.datasheet],
    ["Description", data.description],
  ];
  for (const [key, value] of rows) {
    appendRow(key, value);
  }

  const fp = appState.payload.footprint || {};
  appendRow("Footprint", `${fp.field || ""} / ${fp.pad_count || 0} pads / ${fp.has_3d_model ? "3D" : "no 3D"}`);

  const assets = appState.validation?.assets || appState.payload.assets || deriveAssetManifest(appState.payload);
  appendRow("Symbol Asset", assets.symbol_file);
  appendRow("Footprint Asset", assets.footprint_file);
  appendRow("3D Asset", formatModelAssets(assets));
  if (assets.zip_file_count) {
    appendRow("ZIP Contents", `${assets.zip_file_count} files verified`);
  }
}

function formatModelAssets(assets) {
  if (!assets) return "";
  const files = assets.model_files || [];
  if (files.length) return files.join(", ");
  if (assets.model_name) return `${assets.model_name} (${assets.model_status || "pending"})`;
  return assets.model_status === "none" ? "none" : "";
}

function syncAssetManifest() {
  if (!appState.payload) return;
  appState.payload.assets = deriveAssetManifest(appState.payload);
  queuePersistSession();
}

function deriveAssetManifest(payload) {
  const library = payload?.library || "easyeda2kicad";
  const info = payload?.symbol?.info || {};
  const footprint = payload?.footprint || {};
  const footprintName = footprintNameFromField(info.package || "") || footprint.name || "";
  const modelName = footprint.model_name || "";
  const hasModel = Boolean(footprint.has_3d_model || modelName);
  return {
    library,
    symbol_file: `${library}.kicad_sym`,
    footprint_file: footprintName ? `${library}.pretty/${footprintName}.kicad_mod` : "",
    model_dir: hasModel ? `${library}.3dshapes` : "",
    model_name: modelName,
    model_status: hasModel ? "pending" : "none",
    model_files: [],
    model_refs: [],
    zip_file_count: 0,
  };
}

function footprintNameFromField(value) {
  const cleaned = String(value || "").trim();
  if (!cleaned) return "";
  const parts = cleaned.split(":");
  return parts[parts.length - 1].trim();
}

function renderNotes() {
  notes.innerHTML = "";
  if (!appState.payload) return;
  if (appState.validation) {
    const summary = document.createElement("p");
    summary.className = `note validation-${appState.validation.status || "warn"}`;
    summary.textContent = appState.validation.message || "Validation complete";
    notes.append(summary);
    for (const check of appState.validation.checks || []) {
      const note = document.createElement("p");
      note.className = `note validation-${check.level || "warn"}`;
      note.textContent = check.message || "";
      notes.append(note);
    }
  }

  const currentNotes = appState.payload.symbol?.notes || [];
  for (const text of currentNotes) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = text;
    notes.append(note);
  }
}

function renderKicadPreview() {
  const svg = appState.validation?.svg || "";
  kicadPreviewShell.hidden = !svg;
  kicadPreview.innerHTML = "";
  if (!svg) return;
  kicadPreview.innerHTML = inlineSvg(svg);
}

function renderFootprintPreview() {
  const svg = appState.validation?.footprint_svg || "";
  footprintPreviewShell.hidden = !svg;
  footprintPreview.innerHTML = "";
  if (!svg) return;
  footprintPreview.innerHTML = inlineSvg(svg);
}

function inlineSvg(svg) {
  return String(svg || "")
    .replace(/<\?xml[\s\S]*?\?>\s*/i, "")
    .replace(/<!doctype[\s\S]*?>\s*/i, "");
}

function clearValidation(markDirty = true) {
  if (markDirty) {
    appState.payloadRevision += 1;
    if (appState.payload) {
      setStatus("Edited, validate again");
    }
  }
  appState.validationRevision = -1;
  queuePersistSession();
  if (!appState.validation) {
    updateActionState();
    return;
  }
  appState.validation = null;
  renderNotes();
  renderKicadPreview();
  renderFootprintPreview();
  updateActionState();
}

function renderProperties() {
  propertyForm.innerHTML = "";
  if (!appState.payload) return;
  const info = appState.payload.symbol.info;
  for (const [name, label] of propertyFields) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const lab = document.createElement("label");
    lab.textContent = label;
    lab.htmlFor = `prop-${name}`;
    let field;
    if (name === "description") {
      field = document.createElement("textarea");
    } else {
      field = document.createElement("input");
    }
    field.id = `prop-${name}`;
    field.value = info[name] || "";
    field.addEventListener("input", () => {
      clearValidation();
      info[name] = field.value;
      syncAssetManifest();
      renderOverview();
      renderPreview();
    });
    wrap.append(lab, field);
    propertyForm.append(wrap);
  }
}

function renderCustomFields() {
  customFields.innerHTML = "";
  if (!appState.payload) return;
  const fields = appState.payload.symbol.custom_fields || {};
  for (const [key, value] of Object.entries(fields)) {
    addCustomFieldRow(key, value);
  }
}

function addCustomFieldRow(key = "", value = "") {
  const rowIndex = customFieldCounter;
  customFieldCounter += 1;
  const row = document.createElement("div");
  row.className = "custom-row";
  const keyInput = document.createElement("input");
  const valueInput = document.createElement("input");
  const remove = document.createElement("button");
  keyInput.id = `custom-field-key-${rowIndex}`;
  keyInput.name = "custom_field_key";
  keyInput.value = key;
  keyInput.placeholder = "Field";
  valueInput.id = `custom-field-value-${rowIndex}`;
  valueInput.name = "custom_field_value";
  valueInput.value = value;
  valueInput.placeholder = "Value";
  remove.type = "button";
  remove.className = "ghost";
  remove.textContent = "X";

  const sync = () => {
    clearValidation();
    const fields = {};
    for (const item of customFields.querySelectorAll(".custom-row")) {
      const inputs = item.querySelectorAll("input");
      const itemKey = inputs[0].value.trim();
      if (itemKey) fields[itemKey] = inputs[1].value;
    }
    appState.payload.symbol.custom_fields = fields;
  };
  keyInput.addEventListener("input", sync);
  valueInput.addEventListener("input", sync);
  remove.addEventListener("click", () => {
    row.remove();
    sync();
  });

  row.append(keyInput, valueInput, remove);
  customFields.append(row);
}

function renderPins() {
  pinTableBody.innerHTML = "";
  if (!appState.payload) return;
  const typeValues = appState.payload.pin_type_values || [];
  const styleValues = appState.payload.pin_style_values || [];
  const sideValues = ["top", "left", "right", "bottom"];

  appState.payload.symbol.pins.forEach((pin, index) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const numberCell = document.createElement("td");
    const typeCell = document.createElement("td");
    const styleCell = document.createElement("td");
    const sideCell = document.createElement("td");
    const xCell = document.createElement("td");
    const yCell = document.createElement("td");
    const orientationCell = document.createElement("td");
    const lengthCell = document.createElement("td");
    const name = document.createElement("input");
    const number = document.createElement("input");
    const type = document.createElement("select");
    const style = document.createElement("select");
    const side = document.createElement("select");
    const x = numericInput(pin.x, "0.01");
    const y = numericInput(pin.y, "0.01");
    const orientation = numericInput(pin.orientation, "90");
    const length = numericInput(pin.length, "0.01");
    const fields = {
      name,
      number,
      type,
      style,
      side,
      x,
      y,
      orientation,
      length,
    };

    for (const [fieldName, field] of Object.entries(fields)) {
      field.id = `pin-${index}-${fieldName}`;
      field.name = `pin_${index}_${fieldName}`;
      field.setAttribute("aria-label", `Pin ${index + 1} ${fieldName}`);
    }

    name.value = pin.name || "";
    number.value = pin.number || "";
    for (const value of typeValues) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = pin.type === value;
      type.append(option);
    }
    for (const value of styleValues) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = pin.style === value;
      style.append(option);
    }
    for (const value of sideValues) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = pin.side === value;
      side.append(option);
    }

    name.addEventListener("input", () => {
      clearValidation();
      pin.name = name.value;
      renderPreview();
    });
    number.addEventListener("input", () => {
      clearValidation();
      pin.number = number.value;
      renderPreview();
    });
    type.addEventListener("change", () => {
      clearValidation();
      pin.type = type.value;
      renderPreview();
    });
    style.addEventListener("change", () => {
      clearValidation();
      pin.style = style.value;
      renderPreview();
    });
    side.addEventListener("change", () => {
      clearValidation();
      pin.side = side.value;
      reflowPinsFromSides();
      renderPins();
      renderPreview();
    });
    bindNumberInput(x, (value) => {
      clearValidation();
      pin.x = value;
      renderPreview();
    });
    bindNumberInput(y, (value) => {
      clearValidation();
      pin.y = value;
      renderPreview();
    });
    bindNumberInput(orientation, (value) => {
      clearValidation();
      pin.orientation = normalizeOrientation(value);
      orientation.value = pin.orientation;
      renderPreview();
    });
    bindNumberInput(length, (value) => {
      clearValidation();
      pin.length = Math.max(0.01, value);
      length.value = pin.length;
      renderPreview();
    });

    nameCell.append(name);
    numberCell.append(number);
    typeCell.append(type);
    styleCell.append(style);
    sideCell.append(side);
    xCell.append(x);
    yCell.append(y);
    orientationCell.append(orientation);
    lengthCell.append(length);
    row.dataset.index = index;
    row.append(
      nameCell,
      numberCell,
      typeCell,
      styleCell,
      sideCell,
      xCell,
      yCell,
      orientationCell,
      lengthCell,
    );
    pinTableBody.append(row);
  });
}

function numericInput(value, step) {
  const input = document.createElement("input");
  input.type = "number";
  input.step = step;
  input.value = Number(value || 0);
  return input;
}

function bindNumberInput(input, onValue) {
  input.addEventListener("input", () => {
    const value = Number(input.value);
    if (Number.isFinite(value)) {
      onValue(value);
    }
  });
}

function normalizeOrientation(value) {
  return ((Math.round(value / 90) * 90) % 360 + 360) % 360;
}

function reflowPinsFromSides() {
  if (!appState.payload) return;
  const pins = appState.payload.symbol.pins;
  const groups = {
    top: pins.filter((pin) => pin.side === "top"),
    left: pins.filter((pin) => pin.side === "left"),
    right: pins.filter((pin) => pin.side === "right"),
    bottom: pins.filter((pin) => pin.side === "bottom"),
  };
  const spacing = 2.54;
  const length = 2.54;
  const maxVertical = Math.max(groups.left.length, groups.right.length, 1);
  const maxHorizontal = Math.max(groups.top.length, groups.bottom.length, 1);
  const halfHeight = Math.max(3.81, Math.round(((maxVertical + 1) * spacing) / spacing / 2) * spacing);
  const halfWidth = Math.max(5.08, Math.round(((maxHorizontal + 1) * spacing) / spacing / 2) * spacing);

  const ys = (count) => Array.from({ length: count }, (_, idx) => gridCenteredOffset(count, idx, spacing, -1));
  const xs = (count) => Array.from({ length: count }, (_, idx) => gridCenteredOffset(count, idx, spacing, 1));

  groups.left.forEach((pin, idx) => {
    pin.x = -halfWidth - length;
    pin.y = ys(groups.left.length)[idx];
    pin.orientation = 0;
    pin.length = length;
  });
  groups.right.forEach((pin, idx) => {
    pin.x = halfWidth + length;
    pin.y = ys(groups.right.length)[idx];
    pin.orientation = 180;
    pin.length = length;
  });
  groups.top.forEach((pin, idx) => {
    pin.x = xs(groups.top.length)[idx];
    pin.y = halfHeight + length;
    pin.orientation = 270;
    pin.length = length;
  });
  groups.bottom.forEach((pin, idx) => {
    pin.x = xs(groups.bottom.length)[idx];
    pin.y = -halfHeight - length;
    pin.orientation = 90;
    pin.length = length;
  });
}

function gridCenteredOffset(count, idx, spacing, direction) {
  if (count % 2) {
    const middle = Math.floor(count / 2);
    return (idx - middle) * spacing * direction;
  }
  const half = count / 2;
  const magnitude = idx < half ? half - idx : idx - half + 1;
  const sign = idx < half ? -1 : 1;
  return magnitude * spacing * sign * direction;
}

function renderPreview() {
  preview.innerHTML = "";
  if (!appState.payload) return;

  const pins = appState.payload.symbol.pins || [];
  const scale = 12;
  const maxBodyX = Math.max(5.08, ...pins.map((pin) => Math.max(0, Math.abs(Number(pin.x || 0)) - Number(pin.length || 2.54))));
  const maxBodyY = Math.max(3.81, ...pins.map((pin) => Math.max(0, Math.abs(Number(pin.y || 0)) - Number(pin.length || 2.54))));
  const halfWidth = Math.ceil(maxBodyX / 2.54) * 2.54;
  const halfHeight = Math.ceil(maxBodyY / 2.54) * 2.54;
  appState.previewBounds = {
    x: -Math.max(halfWidth * scale + 120, 220),
    y: -Math.max(halfHeight * scale + 90, 170),
    width: Math.max(halfWidth * 2 * scale + 240, 440),
    height: Math.max(halfHeight * 2 * scale + 180, 340),
  };
  updatePreviewViewBox();
  const sx = (x) => Number(x || 0) * scale;
  const sy = (y) => -Number(y || 0) * scale;

  const body = svg("rect", {
    x: sx(-halfWidth),
    y: sy(halfHeight),
    width: halfWidth * 2 * scale,
    height: halfHeight * 2 * scale,
    class: "preview-body",
  });
  preview.append(body);

  for (const pin of pins) {
    const length = Number(pin.length || 2.54);
    let endX = Number(pin.x || 0);
    let endY = Number(pin.y || 0);
    if (Number(pin.orientation) === 0) endX += length;
    if (Number(pin.orientation) === 180) endX -= length;
    if (Number(pin.orientation) === 90) endY += length;
    if (Number(pin.orientation) === 270) endY -= length;

    const klass = pin.type?.startsWith("power") ? "preview-pin power" : pin.side === "bottom" ? "preview-pin ground" : "preview-pin";
    preview.append(svg("line", {
      x1: sx(pin.x),
      y1: sy(pin.y),
      x2: sx(endX),
      y2: sy(endY),
      class: klass,
    }));

    const labelPad = 7;
    const anchor = Number(pin.orientation) === 180 ? "end" : Number(pin.orientation) === 0 ? "start" : "middle";
    const textX = sx(endX) + (Number(pin.orientation) === 0 ? labelPad : Number(pin.orientation) === 180 ? -labelPad : 0);
    const textY = sy(endY) + (Number(pin.orientation) === 90 ? -labelPad : Number(pin.orientation) === 270 ? labelPad + 8 : 4);
    const numberY = textY + 13;
    preview.append(svgText(pin.name || "~", textX, textY, "preview-label", anchor));
    preview.append(svgText(pin.number || "", textX, numberY, "preview-number", anchor));
  }

  const title = appState.payload.symbol.info.name || "";
  preview.append(svgText(title, 0, sy(0) + 5, "preview-label", "middle"));
}

function updatePreviewViewBox() {
  const bounds = appState.previewBounds;
  const zoom = Math.max(0.35, Math.min(4, appState.previewZoom));
  appState.previewZoom = zoom;
  const width = bounds.width / zoom;
  const height = bounds.height / zoom;
  const centerX = bounds.x + bounds.width / 2 + appState.previewPanX;
  const centerY = bounds.y + bounds.height / 2 + appState.previewPanY;
  preview.setAttribute("viewBox", `${centerX - width / 2} ${centerY - height / 2} ${width} ${height}`);
}

function changePreviewZoom(factor) {
  if (!appState.payload) return;
  appState.previewZoom = Math.max(0.35, Math.min(4, appState.previewZoom * factor));
  updatePreviewViewBox();
}

function resetPreviewZoom() {
  appState.previewZoom = 1;
  appState.previewPanX = 0;
  appState.previewPanY = 0;
  updatePreviewViewBox();
}

function translatePreview(deltaX, deltaY) {
  if (!appState.payload) return;
  appState.previewPanX += deltaX / appState.previewZoom;
  appState.previewPanY += deltaY / appState.previewZoom;
  updatePreviewViewBox();
}

function currentViewBox() {
  const raw = preview.getAttribute("viewBox") || "-220 -170 440 340";
  const [x, y, width, height] = raw.split(/\s+/).map(Number);
  return { x, y, width, height };
}

function svg(tag, attrs) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, value);
  }
  return element;
}

function svgText(text, x, y, klass, anchor) {
  const element = svg("text", {
    x,
    y,
    class: klass,
    "text-anchor": anchor,
  });
  element.textContent = escapeText(text);
  return element;
}

async function downloadJsonBlob(url, filenameFallback) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(libraryExportPayload()),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || response.statusText);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : filenameFallback;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

async function pollCodex(jobId, symbolId = appState.activeSymbolId) {
  if (!jobId) return;
  const item = symbolItemById(symbolId);
  if (!item) return;
  item.activeCodexJobId = jobId;
  item.codexRunning = true;
  item.codexApplied = false;
  if (symbolId === appState.activeSymbolId) {
    appState.activeCodexJobId = jobId;
    appState.codexRunning = true;
    appState.codexApplied = false;
  }
  runCodexButton.disabled = true;
  codexState.textContent = "Codex running";
  setStatus("Codex checking datasheet");
  queuePersistSession();
  for (;;) {
    if (item.activeCodexJobId !== jobId) {
      return;
    }
    let job;
    try {
      const response = await fetch(`/api/codex/${jobId}`);
      job = await response.json();
      if (!response.ok) throw new Error(job.error || response.statusText);
    } catch (error) {
      if (item.activeCodexJobId !== jobId) {
        return;
      }
      item.codexRunning = false;
      item.activeCodexJobId = null;
      if (symbolId === appState.activeSymbolId) {
        appState.codexRunning = false;
        appState.activeCodexJobId = null;
        codexState.textContent = "Codex status error";
        setStatus("Codex status error", true);
        showError(error.message);
        renderAll();
      }
      queuePersistSession();
      return;
    }

    if (item.activeCodexJobId !== jobId) {
      return;
    }

    const message = codexJobMessage(job);
    if (symbolId === appState.activeSymbolId) {
      codexState.textContent = message;
      setStatus(message || "Codex running");
    }
    if (job.status === "complete" && !item.codexApplied) {
      if (item.payload?.codex_job_id !== jobId) {
        item.codexRunning = false;
        item.activeCodexJobId = null;
        if (symbolId === appState.activeSymbolId) {
          appState.codexRunning = false;
          appState.activeCodexJobId = null;
          codexState.textContent = "Codex result stale";
          setStatus("Ignored stale Codex result");
          renderAll();
        }
        queuePersistSession();
        return;
      }
      item.codexApplied = true;
      try {
        const payload = await postJson("/api/codex/apply", {
          payload: item.payload,
          suggestions: job.result,
        });
        item.validation = null;
        item.validationRevision = -1;
        item.payloadRevision = Number(item.payloadRevision || 0) + 1;
        item.payload = payload;
        item.codexRunning = false;
        item.activeCodexJobId = null;
        if (symbolId === appState.activeSymbolId) {
          loadSymbolState(item);
          setStatus(job.result?.status_line || "Codex applied");
          renderAll();
        }
      } catch (error) {
        item.codexRunning = false;
        item.activeCodexJobId = null;
        if (symbolId === appState.activeSymbolId) {
          appState.codexRunning = false;
          appState.activeCodexJobId = null;
          codexState.textContent = "Codex apply failed";
          setStatus("Codex apply failed", true);
          showError(error.message);
          renderAll();
        }
      }
      queuePersistSession();
      return;
    }
    if (!["queued", "running"].includes(job.status)) {
      if (item.activeCodexJobId !== jobId) {
        return;
      }
      item.codexRunning = false;
      item.activeCodexJobId = null;
      if (symbolId === appState.activeSymbolId) {
        appState.codexRunning = false;
        appState.activeCodexJobId = null;
        if (job.error) {
          const note = document.createElement("p");
          note.className = "note error";
          note.textContent = job.error;
          notes.prepend(note);
        }
        setStatus(job.message || "Codex failed", true);
        renderAll();
      }
      queuePersistSession();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

function codexJobMessage(job) {
  const message = job.message || job.result?.status_line || job.status || "Codex running";
  const elapsed = Number(job.elapsed_seconds || 0);
  if (!["queued", "running"].includes(job.status) || !elapsed) {
    return message;
  }
  const elapsedText = formatElapsed(elapsed);
  return message.includes(elapsedText) ? message : `${message} (${elapsedText})`;
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  if (minutes) {
    return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  }
  return `${remainder}s`;
}

async function startCodexForCurrentPayload() {
  if (!appState.payload || appState.codexRunning) return;
  const item = activeSymbolItem();
  if (!item) return;
  syncActiveUnit();
  setStatus("Starting Codex");
  codexState.textContent = "Starting Codex";
  appState.codexApplied = false;
  appState.codexRunning = true;
  item.codexApplied = false;
  item.codexRunning = true;
  renderAll();
  try {
    const job = await postJson("/api/codex/start", { payload: appState.payload });
    appState.payload.codex_job_id = job.job_id;
    appState.activeCodexJobId = job.job_id;
    item.payload = appState.payload;
    item.activeCodexJobId = job.job_id;
    queuePersistSession();
    await pollCodex(job.job_id, item.id);
  } catch (error) {
    appState.codexRunning = false;
    appState.activeCodexJobId = null;
    item.codexRunning = false;
    item.activeCodexJobId = null;
    codexState.textContent = "Codex start failed";
    setStatus("Codex start failed", true);
    showError(error.message);
    renderAll();
    queuePersistSession();
  }
}

async function pollImport(job, lcscId, index, total) {
  let current = job;
  const jobId = current.job_id;
  if (!jobId) {
    throw new Error("Import job did not return an id.");
  }
  for (;;) {
    const prefix = total > 1 ? `${index}/${total} ${lcscId}: ` : "";
    setStatus(prefix + (current.message || current.status || "Importing"));
    if (current.status === "complete") {
      return current.result;
    }
    if (!["queued", "running"].includes(current.status)) {
      throw new Error(current.error || current.message || "Import failed");
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
    const response = await fetch(`/api/import/${jobId}`);
    current = await response.json();
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const ids = parseImportIds(lcscInput.value);
  if (!ids.length) {
    setStatus("Enter at least one LCSC part", true);
    return;
  }
  setStatus(ids.length > 1 ? `Starting ${ids.length} imports` : "Starting import");
  codexState.textContent = "No job";
  saveActiveSymbolState();
  const folder = createImportFolder(ids);
  renderAll();
  let imported = 0;
  try {
    for (const [idx, lcscId] of ids.entries()) {
      const job = await postJson("/api/import", {
        lcsc_id: lcscId,
        run_codex: codexPass.checked,
      });
      const payload = await pollImport(job, lcscId, idx + 1, ids.length);
      const item = addPayloadToLibrary(payload, folder.id);
      imported += 1;
      setStatus(`Imported ${lcscId}`);
      renderAll();
      queuePersistSession();
      if (payload.codex_job_id) {
        codexState.textContent = `Codex queued ${payload.codex_job_id.slice(0, 8)}`;
        await pollCodex(payload.codex_job_id, item.id);
      }
    }
    setStatus(imported === 1 ? "Import ready" : `Imported ${imported} symbols`);
  } catch (error) {
    setStatus("Error", true);
    showError(error.message);
  } finally {
    renderAll();
    queuePersistSession();
  }
});

cleanupButton.addEventListener("click", async () => {
  if (!appState.payload) return;
  syncActiveUnit();
  setStatus("Cleaning");
  try {
    appState.payload = await postJson("/api/cleanup", appState.payload);
    appState.validation = null;
    appState.validationRevision = -1;
    appState.payloadRevision += 1;
    setStatus("Ready");
    renderAll();
    queuePersistSession();
  } catch (error) {
    setStatus("Error", true);
    showError(error.message);
  }
});

validateButton.addEventListener("click", async () => {
  if (!appState.payload) return;
  syncActiveUnit();
  setStatus("Validating");
  appState.validation = null;
  appState.validationRevision = -1;
  updateActionState();
  const requestedRevision = appState.payloadRevision;
  try {
    const validation = await postJson("/api/validate", appState.payload);
    if (requestedRevision !== appState.payloadRevision) {
      setStatus("Validation stale");
      updateActionState();
      return;
    }
    appState.validation = validation;
    appState.validationRevision = requestedRevision;
    setStatus(appState.validation.message || "Validation complete", appState.validation.status === "error");
    renderAll();
    queuePersistSession();
  } catch (error) {
    setStatus("Validation failed", true);
    showError(error.message);
  }
});

addFieldButton.addEventListener("click", () => {
  if (!appState.payload) return;
  addCustomFieldRow("", "");
});

runCodexButton.addEventListener("click", startCodexForCurrentPayload);

symbolSelect.addEventListener("change", () => {
  setActiveSymbol(symbolSelect.value);
});

unitSelect.addEventListener("change", () => {
  switchActiveUnit(Number(unitSelect.value || 0));
});

clearLibraryButton.addEventListener("click", () => {
  cancelCodexJobs();
  appState.library = { version: 1, library: "easyeda2kicad", folders: [], symbols: [] };
  appState.activeFolderId = null;
  appState.activeSymbolId = null;
  appState.payload = null;
  appState.validation = null;
  appState.validationRevision = -1;
  appState.payloadRevision = 0;
  appState.previewZoom = 1;
  appState.previewPanX = 0;
  appState.previewPanY = 0;
  localStorage.removeItem(STORAGE_KEY);
  setStatus("Library cleared");
  renderAll();
});

downloadSymbolButton.addEventListener("click", async () => {
  if (!appState.library.symbols.length) {
    setStatus("Import before download", true);
    return;
  }
  setStatus("Exporting");
  try {
    await downloadJsonBlob("/api/export/symbol", "symbol.kicad_sym");
    setStatus("Ready");
  } catch (error) {
    setStatus("Error", true);
    showError(error.message);
  }
});

downloadBundleButton.addEventListener("click", async () => {
  if (!appState.library.symbols.length) {
    setStatus("Import before download", true);
    return;
  }
  setStatus("Bundling");
  try {
    await downloadJsonBlob("/api/export/bundle", "kicad_assets.zip");
    setStatus("Ready");
  } catch (error) {
    setStatus("Error", true);
    showError(error.message);
  }
});

themeToggle.addEventListener("click", () => {
  document.body.classList.toggle("dark");
  themeToggle.textContent = document.body.classList.contains("dark") ? "Light" : "Dark";
});

zoomOutButton.addEventListener("click", () => changePreviewZoom(1 / 1.25));
zoomInButton.addEventListener("click", () => changePreviewZoom(1.25));
zoomResetButton.addEventListener("click", resetPreviewZoom);
panLeftButton.addEventListener("click", () => translatePreview(-36, 0));
panRightButton.addEventListener("click", () => translatePreview(36, 0));
panUpButton.addEventListener("click", () => translatePreview(0, -36));
panDownButton.addEventListener("click", () => translatePreview(0, 36));

preview.addEventListener("wheel", (event) => {
  if (!appState.payload) return;
  event.preventDefault();
  changePreviewZoom(event.deltaY < 0 ? 1.12 : 1 / 1.12);
}, { passive: false });

preview.addEventListener("pointerdown", (event) => {
  if (!appState.payload || event.button !== 0) return;
  const viewBox = currentViewBox();
  appState.previewDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    panX: appState.previewPanX,
    panY: appState.previewPanY,
    unitX: viewBox.width / Math.max(preview.clientWidth, 1),
    unitY: viewBox.height / Math.max(preview.clientHeight, 1),
  };
  preview.classList.add("is-panning");
  preview.setPointerCapture(event.pointerId);
});

preview.addEventListener("pointermove", (event) => {
  const drag = appState.previewDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const deltaX = (event.clientX - drag.startX) * drag.unitX;
  const deltaY = (event.clientY - drag.startY) * drag.unitY;
  appState.previewPanX = drag.panX - deltaX;
  appState.previewPanY = drag.panY - deltaY;
  updatePreviewViewBox();
});

function endPreviewDrag(event) {
  const drag = appState.previewDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  appState.previewDrag = null;
  preview.classList.remove("is-panning");
  try {
    preview.releasePointerCapture(event.pointerId);
  } catch (error) {
    // Pointer capture can already be gone if the pointer was cancelled.
  }
}

preview.addEventListener("pointerup", endPreviewDrag);
preview.addEventListener("pointercancel", endPreviewDrag);

window.addEventListener("beforeunload", () => {
  navigator.sendBeacon?.("/api/codex/cancel_all", new Blob(["{}"], { type: "application/json" }));
});

restoreSession();
cancelCodexJobs();
renderAll();
