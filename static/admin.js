// Админка: ручная загрузка моделей папкой или архивом.
//
// Браузер в multipart отдаёт только имя файла, поэтому структуру папки шлём
// параллельным массивом `paths` — иначе на сервере текстуры потеряют вложенность
// и модель откроется без материалов.

let selectedFiles = [];

const $ = (id) => document.getElementById(id);

function showTab(id) {
  document.querySelectorAll(".admin-tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === id);
  });
  document.querySelectorAll(".admin-panel").forEach((p) => {
    p.hidden = p.dataset.panel !== id;
  });
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  if (id === "metrics") loadMetrics();
  if (id === "catalog") {
    loadStats();
    loadModels();
  }
  if (id === "plans" || id === "customers") loadBilling();
  if (id === "keys") loadApiKeys();
}

document.getElementById("admin-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) showTab(btn.dataset.tab);
});

function formatSize(bytes) {
  if (!bytes) return "0 Б";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

async function loadStats() {
  try {
    const res = await fetch("/api/admin/stats");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const s = await res.json();
    $("stats").innerHTML = "";
    const cells = [
      ["Моделей", s.total.toLocaleString("ru")],
      ["Классифицировано", s.classified.toLocaleString("ru")],
      ["С описанием", s.described.toLocaleString("ru")],
      ["В Qdrant", s.embedded.toLocaleString("ru")],
      ["Ждут разметки", s.pending_classification.toLocaleString("ru")],
      ["Объём", formatSize(s.total_bytes)],
    ];
    for (const [label, value] of cells) {
      const el = document.createElement("div");
      el.className = "stat";
      el.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
      el.querySelector(".stat-value").textContent = value;
      el.querySelector(".stat-label").textContent = label;
      $("stats").appendChild(el);
    }
  } catch (e) {
    $("stats").textContent = `Не удалось получить статистику: ${e.message}`;
  }
}

async function loadCategories() {
  const sel = $("category");
  const modelSel = $("model-category");
  try {
    const res = await fetch("/api/categories");
    const data = await res.json();
    sel.innerHTML = "";
    if (modelSel) {
      modelSel.innerHTML = `<option value="">все</option>`;
    }
    for (const c of data.categories) {
      const opt = document.createElement("option");
      opt.value = c.category;
      opt.textContent = c.count ? `${c.category} (${c.count})` : c.category;
      sel.appendChild(opt);
      if (modelSel) modelSel.appendChild(opt.cloneNode(true));
    }
    sel.value = "prop";
  } catch {
    sel.innerHTML = '<option value="other">other</option>';
  }
}

function setSelection(files) {
  selectedFiles = files;
  const box = $("selected");
  if (!files.length) {
    box.hidden = true;
    $("do-upload").disabled = true;
    $("clear-selection").hidden = true;
    return;
  }
  const total = files.reduce((sum, f) => sum + f.size, 0);
  const names = files.slice(0, 6).map((f) => f.webkitRelativePath || f.name);
  box.hidden = false;
  box.innerHTML = "";
  const head = document.createElement("p");
  head.className = "selected-head";
  head.textContent = `Выбрано файлов: ${files.length} — ${formatSize(total)}`;
  box.appendChild(head);
  const list = document.createElement("ul");
  list.className = "selected-list";
  for (const n of names) {
    const li = document.createElement("li");
    li.textContent = n;
    list.appendChild(li);
  }
  if (files.length > names.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = `…и ещё ${files.length - names.length}`;
    list.appendChild(li);
  }
  box.appendChild(list);
  $("do-upload").disabled = false;
  $("clear-selection").hidden = false;
}

function showResult(data, isError = false) {
  const box = $("result");
  box.hidden = false;
  box.className = `result ${isError ? "error" : "ok"}`;
  box.innerHTML = "";

  if (isError) {
    box.textContent = typeof data === "string" ? data : JSON.stringify(data);
    return;
  }

  const lines = [];
  if (data.uploaded) lines.push(`Загружено моделей: ${data.uploaded}`);
  if (data.duplicates) lines.push(`Пропущено дублей (по контрольной сумме): ${data.duplicates}`);
  if (data.already_known) lines.push(`Уже были в каталоге: ${data.already_known}`);
  if (data.failed) lines.push(`Не удалось: ${data.failed}`);
  if (data.bytes) lines.push(`Залито в хранилище: ${formatSize(data.bytes)}`);
  if (data.category) lines.push(`Категория: ${data.category}`);
  if (!lines.length && data.error) lines.push(data.error);

  for (const line of lines) {
    const p = document.createElement("p");
    p.textContent = line;
    box.appendChild(p);
  }
  for (const note of data.skipped || []) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = `Пропущено — ${note}`;
    box.appendChild(p);
  }
  for (const err of data.errors || []) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = err;
    box.appendChild(p);
  }
}

function upload() {
  if (!selectedFiles.length) return;
  const form = new FormData();
  for (const f of selectedFiles) {
    form.append("files", f, f.name);
    // Относительный путь нужен, чтобы сохранить structure textures/… на сервере.
    form.append("paths", f.webkitRelativePath || f.name);
  }
  form.append("category", $("category").value);

  // XMLHttpRequest, а не fetch: только он даёт прогресс отправки, а файлы тут
  // бывают в сотни мегабайт — без индикатора непонятно, идёт ли загрузка.
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/admin/upload");

  $("progress-wrap").hidden = false;
  $("do-upload").disabled = true;
  $("result").hidden = true;

  xhr.upload.addEventListener("progress", (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    $("progress-fill").style.width = `${pct}%`;
    $("progress-text").textContent =
      pct < 100 ? `${pct}% — ${formatSize(e.loaded)} из ${formatSize(e.total)}`
                : "Обработка на сервере…";
  });

  xhr.addEventListener("load", () => {
    $("do-upload").disabled = false;
    let data;
    try {
      data = JSON.parse(xhr.responseText);
    } catch {
      showResult(`Некорректный ответ сервера (HTTP ${xhr.status})`, true);
      return;
    }
    if (xhr.status >= 400) {
      showResult(data.detail || `Ошибка HTTP ${xhr.status}`, true);
      return;
    }
    showResult(data, data.ok === false && !data.uploaded);
    setSelection([]);
    $("progress-wrap").hidden = true;
    $("progress-fill").style.width = "0%";
    loadStats();
  });

  xhr.addEventListener("error", () => {
    $("do-upload").disabled = false;
    $("progress-wrap").hidden = true;
    showResult("Сеть недоступна или загрузка прервана", true);
  });

  xhr.send(form);
}

// --- drag & drop ---------------------------------------------------------- //
async function filesFromDataTransfer(dt) {
  const out = [];
  const items = Array.from(dt.items || []);
  const entries = items
    .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
    .filter(Boolean);

  if (!entries.length) return Array.from(dt.files || []);

  async function walk(entry, prefix) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      // webkitRelativePath у перетащенных файлов пустой — проставляем сами.
      Object.defineProperty(file, "webkitRelativePath", {
        value: prefix + entry.name,
        configurable: true,
      });
      out.push(file);
      return;
    }
    const reader = entry.createReader();
    const children = await new Promise((res) => reader.readEntries(res));
    for (const child of children) await walk(child, `${prefix}${entry.name}/`);
  }

  for (const entry of entries) await walk(entry, "");
  return out;
}

const dz = $("dropzone");
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    dz.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
  })
);
dz.addEventListener("drop", async (e) => {
  setSelection(await filesFromDataTransfer(e.dataTransfer));
});

$("pick-folder").addEventListener("click", () => $("input-folder").click());
$("pick-archive").addEventListener("click", () => $("input-archive").click());
$("input-folder").addEventListener("change", (e) => setSelection(Array.from(e.target.files)));
$("input-archive").addEventListener("change", (e) => setSelection(Array.from(e.target.files)));
$("clear-selection").addEventListener("click", () => setSelection([]));
$("do-upload").addEventListener("click", upload);

loadStats();
loadCategories();
loadBilling();
loadApiKeys();
loadMetrics();
const initialTab = (location.hash || "#metrics").slice(1);
showTab(initialTab || "metrics");

function formatTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ru");
}

async function loadApiKeys() {
  const err = $("keys-error");
  err.hidden = true;
  try {
    const res = await fetch("/api/admin/api-keys");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    fillCustomerSelect($("key-customer"), data.customers || [], true);
    const tbody = $("keys-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!data.keys.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="6" class="muted">Ключей пока нет</td>`;
      tbody.appendChild(tr);
      return;
    }
    for (const key of data.keys) {
      const tr = document.createElement("tr");
      if (key.revoked) tr.className = "revoked";
      const scopes = (key.scopes || []).join(", ");
      tr.innerHTML = `<td></td><td></td><td><code></code></td><td></td><td></td><td></td>`;
      tr.children[0].textContent = key.customer_name || "—";
      tr.children[1].textContent = key.name;
      tr.children[2].querySelector("code").textContent = `${key.key_prefix}…`;
      tr.children[3].textContent = scopes;
      tr.children[4].textContent = `${key.request_count} · ${formatTime(key.last_used_at)}`;
      const actions = document.createElement("div");
      actions.className = "row-actions";
      if (!key.revoked) {
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "ghost";
        edit.textContent = "Изменить";
        edit.addEventListener("click", () => fillKeyForm(key));
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = "Отозвать";
        btn.addEventListener("click", () => revokeApiKey(key.id, key.name));
        actions.appendChild(edit);
        actions.appendChild(btn);
        tr.children[5].appendChild(actions);
      } else {
        tr.children[5].textContent = "отозван";
      }
      tbody.appendChild(tr);
    }
  } catch (e) {
    err.hidden = false;
    err.textContent = `Не удалось загрузить ключи: ${e.message}`;
  }
}

async function revokeApiKey(id, name) {
  if (!confirm(`Отозвать ключ «${name}»? Клиент сразу потеряет доступ.`)) return;
  const res = await fetch(`/api/admin/api-keys/${id}`, { method: "DELETE" });
  if (!res.ok) {
    $("keys-error").hidden = false;
    $("keys-error").textContent = `Не удалось отозвать ключ (HTTP ${res.status})`;
    return;
  }
  loadApiKeys();
}

$("key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("key-name").value.trim();
  const scopes = Array.from(document.querySelectorAll('input[name="scope"]:checked')).map(
    (el) => el.value
  );
  const rate_limit_per_min = Number($("key-limit").value) || 0;
  const customer_id = $("key-customer").value ? Number($("key-customer").value) : null;
  const keyId = $("key-id").value;
  const box = $("new-key");
  box.hidden = true;
  if (keyId) {
    const res = await fetch(`/api/admin/api-keys/${keyId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, scopes, rate_limit_per_min }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("keys-error").hidden = false;
      $("keys-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
      return;
    }
    resetKeyForm();
    loadApiKeys();
    return;
  }
  const res = await fetch("/api/admin/api-keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, scopes, rate_limit_per_min, customer_id }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("keys-error").hidden = false;
    $("keys-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  $("key-name").value = "";
  box.hidden = false;
  box.innerHTML = "";
  const p = document.createElement("p");
  p.textContent = data.warning || "Скопируйте ключ — он больше не будет показан.";
  const code = document.createElement("code");
  code.className = "key-secret";
  code.textContent = data.key;
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "ghost";
  copy.textContent = "Скопировать";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(data.key);
      copy.textContent = "Скопировано";
    } catch {
      copy.textContent = "Не удалось скопировать";
    }
  });
  box.appendChild(p);
  box.appendChild(code);
  box.appendChild(copy);
  loadApiKeys();
});

const GB = 1024 * 1024 * 1024;

function formatRub(cents) {
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    maximumFractionDigits: 0,
  }).format((cents || 0) / 100);
}

function formatQuota(used, limit) {
  if (!limit) return `${used} / ∞`;
  return `${used} / ${limit}`;
}

function fillCustomerSelect(sel, customers, withEmpty) {
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = withEmpty
    ? `<option value="">без биллинга (внутренний ключ)</option>`
    : "";
  for (const c of customers) {
    const opt = document.createElement("option");
    opt.value = String(c.id);
    opt.textContent = c.name;
    sel.appendChild(opt);
  }
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = `badge badge-${status || "none"}`;
  span.textContent = status || "нет";
  return span;
}

function planLimits(plan) {
  const lines = [];
  lines.push(plan.requests_per_period ? `${plan.requests_per_period} запросов` : "запросы без лимита");
  lines.push(plan.downloads_per_period ? `${plan.downloads_per_period} скачиваний` : "скачивания без лимита");
  if (plan.searches_per_period) lines.push(`${plan.searches_per_period} семантика`);
  else lines.push("без семантики");
  lines.push(`${plan.rate_limit_per_min}/мин`);
  return lines;
}

function fillPlanForm(plan) {
  $("plan-id").value = plan?.id || "";
  $("plan-slug").value = plan?.slug || "";
  $("plan-name").value = plan?.name || "";
  $("plan-price").value = plan ? (plan.price_cents || 0) / 100 : 0;
  $("plan-period").value = plan?.period || "month";
  $("plan-requests").value = plan?.requests_per_period ?? 1000;
  $("plan-downloads").value = plan?.downloads_per_period ?? 50;
  $("plan-gb").value = plan ? ((plan.bytes_per_period || 0) / GB).toFixed(plan.bytes_per_period ? 2 : 0) : 0.5;
  $("plan-searches").value = plan?.searches_per_period ?? 0;
  $("plan-rpm").value = plan?.rate_limit_per_min ?? 60;
  $("plan-keys").value = plan?.max_api_keys ?? 1;
  $("plan-page").value = plan?.max_page_size ?? 24;
  $("plan-trial").value = plan?.trial_days ?? 0;
  $("plan-desc").value = plan?.description || "";
  $("plan-active").checked = plan ? !!plan.is_active : true;
  const scopes = new Set(plan?.scopes || ["catalog", "download"]);
  document.querySelectorAll('input[name="plan-scope"]').forEach((el) => {
    el.checked = scopes.has(el.value);
  });
  $("plan-delete").hidden = !plan?.id;
  $("plan-save").textContent = plan?.id ? "Сохранить тариф" : "Создать тариф";
}

async function loadBilling() {
  const err = $("customers-error");
  const planErr = $("plans-error");
  err.hidden = true;
  planErr.hidden = true;
  try {
    const res = await fetch("/api/admin/billing");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const yookassaOn = Boolean(data.yookassa && data.yookassa.configured);
    const ykHint = $("yookassa-hint");
    if (ykHint) {
      ykHint.textContent = yookassaOn
        ? "«ЮKassa» создаёт платёж и копирует ссылку для клиента. «Оплачен» — ручное закрытие. Webhook: /api/billing/yookassa/webhook"
        : "ЮKassa выключена: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY. Пока счета закрываются кнопкой «Оплачен».";
    }
    const ov = data.overview || {};
    $("billing-stats").innerHTML = "";
    const cells = [
      ["Клиентов", ov.customers],
      ["Активных подписок", ov.active_subscriptions],
      ["Просрочено", ov.past_due],
      ["К оплате", formatRub(ov.unpaid_cents)],
      ["Оплачено за 30 дней", formatRub(ov.paid_last_30d_cents)],
    ];
    for (const [label, value] of cells) {
      const el = document.createElement("div");
      el.className = "stat";
      el.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
      el.querySelector(".stat-value").textContent = value;
      el.querySelector(".stat-label").textContent = label;
      $("billing-stats").appendChild(el);
    }

    const grid = $("plans-grid");
    grid.innerHTML = "";
    for (const plan of data.plans || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `plan-card${plan.is_active ? "" : " inactive"}`;
      const h = document.createElement("h3");
      h.textContent = plan.name;
      const price = document.createElement("div");
      price.className = "price";
      const period = plan.period === "year" ? "/ год" : plan.period === "once" ? " разово" : "/ мес";
      price.textContent = `${plan.price_label || formatRub(plan.price_cents)}${period}`;
      const ul = document.createElement("ul");
      for (const line of planLimits(plan)) {
        const li = document.createElement("li");
        li.textContent = line;
        ul.appendChild(li);
      }
      btn.appendChild(h);
      btn.appendChild(price);
      btn.appendChild(ul);
      btn.addEventListener("click", () => fillPlanForm(plan));
      grid.appendChild(btn);
    }

    const planSel = $("sub-plan");
    planSel.innerHTML = "";
    for (const plan of (data.plans || []).filter((p) => p.is_active)) {
      const opt = document.createElement("option");
      opt.value = String(plan.id);
      opt.textContent = `${plan.name} · ${plan.price_label}`;
      planSel.appendChild(opt);
    }
    fillCustomerSelect($("sub-customer"), data.customers || [], false);

    const tbody = $("customers-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!(data.customers || []).length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="6" class="muted">Клиентов пока нет</td>`;
      tbody.appendChild(tr);
    }
    for (const c of data.customers || []) {
      const tr = document.createElement("tr");
      const sub = c.subscription;
      tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td>`;
      tr.children[0].textContent = c.email ? `${c.name} · ${c.email}` : c.name;
      tr.children[1].textContent = sub?.plan?.name || "—";
      tr.children[2].appendChild(statusBadge(sub?.status));
      tr.children[3].textContent = sub ? `${formatTime(sub.period_start)} → ${formatTime(sub.period_end)}` : "—";
      tr.children[4].textContent = sub
        ? `запр. ${formatQuota(sub.usage_requests, sub.plan?.requests_per_period)} · ск. ${formatQuota(sub.usage_downloads, sub.plan?.downloads_per_period)}`
        : "—";
      const actions = document.createElement("div");
      actions.className = "row-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "ghost";
      edit.textContent = "Изменить";
      edit.addEventListener("click", () => fillCustomerForm(c));
      actions.appendChild(edit);
      if (sub && sub.status !== "canceled") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = "Отменить";
        btn.addEventListener("click", () => cancelSub(sub.id, c.name));
        actions.appendChild(btn);
      }
      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost";
      del.textContent = "Удалить";
      del.addEventListener("click", () => deleteCustomer(c.id, c.name));
      actions.appendChild(del);
      tr.children[5].appendChild(actions);
      tbody.appendChild(tr);
    }

    const ibody = $("invoices-table").querySelector("tbody");
    ibody.innerHTML = "";
    if (!(data.invoices || []).length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="6" class="muted">Счетов нет</td>`;
      ibody.appendChild(tr);
    }
    for (const inv of data.invoices || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td>`;
      tr.children[0].textContent = formatTime(inv.issued_at);
      tr.children[1].textContent = inv.customer_name || inv.customer_id;
      tr.children[2].textContent = inv.plan_name || "—";
      tr.children[3].textContent = formatRub(inv.amount_cents);
      tr.children[4].appendChild(statusBadge(inv.status));
      const actions = document.createElement("div");
      actions.className = "row-actions";
      if (inv.status === "issued") {
        if (yookassaOn) {
          const yk = document.createElement("button");
          yk.type = "button";
          yk.className = "ghost";
          yk.textContent = inv.yookassa_confirmation_url ? "Ссылка ЮKassa" : "ЮKassa";
          yk.addEventListener("click", () => yookassaPay(inv.id));
          actions.appendChild(yk);
        }
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = "Оплачен";
        btn.addEventListener("click", () => payInvoice(inv.id));
        actions.appendChild(btn);
        const voidBtn = document.createElement("button");
        voidBtn.type = "button";
        voidBtn.className = "ghost";
        voidBtn.textContent = "Аннулировать";
        voidBtn.addEventListener("click", () => voidInvoice(inv.id));
        actions.appendChild(voidBtn);
      }
      if (inv.yookassa_status) {
        const ykStatus = document.createElement("span");
        ykStatus.className = "muted";
        ykStatus.textContent = `ЮKassa: ${inv.yookassa_status}`;
        actions.appendChild(ykStatus);
      }
      tr.children[5].appendChild(actions);
      ibody.appendChild(tr);
    }
  } catch (e) {
    err.hidden = false;
    err.textContent = `Не удалось загрузить биллинг: ${e.message}`;
  }
}

async function cancelSub(id, name) {
  if (!confirm(`Отменить подписку клиента «${name}»?`)) return;
  const res = await fetch(`/api/admin/billing/subscriptions/${id}/cancel`, { method: "POST" });
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = `Не удалось отменить (HTTP ${res.status})`;
    return;
  }
  loadBilling();
}

async function payInvoice(id) {
  const res = await fetch(`/api/admin/billing/invoices/${id}/pay`, { method: "POST" });
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = `Не удалось отметить оплату (HTTP ${res.status})`;
    return;
  }
  loadBilling();
}

async function yookassaPay(id) {
  const res = await fetch(`/api/admin/billing/invoices/${id}/yookassa`, { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = data.detail || `ЮKassa: HTTP ${res.status}`;
    return;
  }
  const link = data.pay_url || data.confirmation_url;
  if (link) {
    try {
      await navigator.clipboard.writeText(link);
    } catch (_) {
      /* clipboard может быть недоступен без HTTPS */
    }
    const openCheckout = data.confirmation_url && confirm(
      `Ссылка на оплату скопирована:\n${link}\n\nОткрыть страницу ЮKassa?`
    );
    if (openCheckout) window.open(data.confirmation_url, "_blank", "noopener");
  }
  loadBilling();
}

$("plan-reset").addEventListener("click", () => fillPlanForm(null));

$("plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const gb = Number($("plan-gb").value) || 0;
  const body = {
    id: $("plan-id").value ? Number($("plan-id").value) : null,
    slug: $("plan-slug").value.trim(),
    name: $("plan-name").value.trim(),
    description: $("plan-desc").value.trim(),
    price_rub: Number($("plan-price").value) || 0,
    period: $("plan-period").value,
    requests_per_period: Number($("plan-requests").value) || 0,
    downloads_per_period: Number($("plan-downloads").value) || 0,
    bytes_per_period: Math.round(gb * GB),
    searches_per_period: Number($("plan-searches").value) || 0,
    rate_limit_per_min: Number($("plan-rpm").value) || 0,
    max_api_keys: Number($("plan-keys").value) || 0,
    max_page_size: Number($("plan-page").value) || 24,
    trial_days: Number($("plan-trial").value) || 0,
    is_active: $("plan-active").checked,
    scopes: Array.from(document.querySelectorAll('input[name="plan-scope"]:checked')).map((el) => el.value),
  };
  const res = await fetch("/api/admin/billing/plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("plans-error").hidden = false;
    $("plans-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  $("plans-error").hidden = true;
  fillPlanForm(null);
  loadBilling();
});

$("customer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("cust-id").value;
  const payload = {
    name: $("cust-name").value.trim(),
    email: $("cust-email").value.trim(),
    notes: $("cust-notes").value.trim(),
  };
  const url = id ? `/api/admin/billing/customers/${id}` : "/api/admin/billing/customers";
  const res = await fetch(url, {
    method: id ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  resetCustomerForm();
  loadBilling();
  loadApiKeys();
});

$("subscribe-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const customerId = $("sub-customer").value;
  if (!customerId) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = "Сначала добавьте клиента";
    return;
  }
  const res = await fetch(`/api/admin/billing/customers/${customerId}/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      plan_id: Number($("sub-plan").value),
      mark_paid: $("sub-paid").checked,
      use_trial: $("sub-trial").checked,
      auto_renew: $("sub-renew").checked,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  loadBilling();
});

function fillKeyForm(key) {
  $("key-id").value = key.id;
  $("key-name").value = key.name;
  $("key-limit").value = key.rate_limit_per_min || 120;
  $("key-customer").value = key.customer_id ? String(key.customer_id) : "";
  $("key-customer").disabled = true;
  const scopes = new Set(key.scopes || []);
  document.querySelectorAll('input[name="scope"]').forEach((el) => {
    el.checked = scopes.has(el.value);
  });
  $("key-save").textContent = "Сохранить ключ";
  $("key-reset").hidden = false;
}

function resetKeyForm() {
  $("key-id").value = "";
  $("key-name").value = "";
  $("key-limit").value = 120;
  $("key-customer").disabled = false;
  $("key-save").textContent = "Создать ключ";
  $("key-reset").hidden = true;
  $("new-key").hidden = true;
}

function fillCustomerForm(c) {
  $("cust-id").value = c.id;
  $("cust-name").value = c.name || "";
  $("cust-email").value = c.email || "";
  $("cust-notes").value = c.notes || "";
  $("cust-save").textContent = "Сохранить";
  $("cust-reset").hidden = false;
}

function resetCustomerForm() {
  $("cust-id").value = "";
  $("cust-name").value = "";
  $("cust-email").value = "";
  $("cust-notes").value = "";
  $("cust-save").textContent = "Добавить клиента";
  $("cust-reset").hidden = true;
}

$("key-reset").addEventListener("click", resetKeyForm);
$("cust-reset").addEventListener("click", resetCustomerForm);

$("plan-delete").addEventListener("click", async () => {
  const id = $("plan-id").value;
  if (!id) return;
  if (!confirm("Удалить этот тариф?")) return;
  const res = await fetch(`/api/admin/billing/plans/${id}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("plans-error").hidden = false;
    $("plans-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  fillPlanForm(null);
  loadBilling();
});

async function deleteCustomer(id, name) {
  if (!confirm(`Удалить клиента «${name}»? Подписка будет отменена, ключи отозваны.`)) return;
  const res = await fetch(`/api/admin/billing/customers/${id}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = data.detail || `Ошибка HTTP ${res.status}`;
    return;
  }
  resetCustomerForm();
  loadBilling();
  loadApiKeys();
}

async function voidInvoice(id) {
  if (!confirm("Аннулировать счёт?")) return;
  const res = await fetch(`/api/admin/billing/invoices/${id}/void`, { method: "POST" });
  if (!res.ok) {
    $("customers-error").hidden = false;
    $("customers-error").textContent = `Не удалось аннулировать (HTTP ${res.status})`;
    return;
  }
  loadBilling();
}

function fillStatsGrid(el, cells) {
  el.innerHTML = "";
  for (const [label, value] of cells) {
    const node = document.createElement("div");
    node.className = "stat";
    node.innerHTML = `<span class="stat-value"></span><span class="stat-label"></span>`;
    node.querySelector(".stat-value").textContent = value;
    node.querySelector(".stat-label").textContent = label;
    el.appendChild(node);
  }
}

async function loadMetrics() {
  try {
    const res = await fetch("/api/admin/metrics?days=14");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const m = await res.json();
    const t = m.totals || {};
    fillStatsGrid($("metrics-stats"), [
      ["Запросы за 14 дней", (t.requests || 0).toLocaleString("ru")],
      ["Скачивания", (t.downloads || 0).toLocaleString("ru")],
      ["Семантика", (t.searches || 0).toLocaleString("ru")],
      ["Трафик", formatSize(t.bytes || 0)],
      ["Сегодня", (m.today?.requests || 0).toLocaleString("ru")],
      ["Активных ключей", `${m.keys_active} / ${m.keys_total}`],
    ]);
    const chart = $("metrics-chart");
    chart.innerHTML = "";
    chart.classList.add("chart-wrap");
    const max = Math.max(1, ...((m.series || []).map((d) => d.requests)));
    for (const d of m.series || []) {
      const col = document.createElement("div");
      col.className = "chart-col";
      col.style.height = `${Math.max(2, (d.requests / max) * 100)}%`;
      col.title = `${d.day}: ${d.requests} запросов, ${d.downloads} скачиваний`;
      const label = document.createElement("span");
      label.textContent = d.day.slice(8);
      col.appendChild(label);
      chart.appendChild(col);
    }
    const catBody = $("top-customers-table").querySelector("tbody");
    catBody.innerHTML = "";
    if (!(m.top_customers || []).length) {
      catBody.innerHTML = `<tr><td colspan="3" class="muted">Пока нет данных — появятся после запросов /v1</td></tr>`;
    }
    for (const row of m.top_customers || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td></td><td></td><td></td>`;
      tr.children[0].textContent = row.name;
      tr.children[1].textContent = row.requests.toLocaleString("ru");
      tr.children[2].textContent = row.downloads.toLocaleString("ru");
      catBody.appendChild(tr);
    }
    const keyBody = $("top-keys-table").querySelector("tbody");
    keyBody.innerHTML = "";
    if (!(m.top_keys || []).length) {
      keyBody.innerHTML = `<tr><td colspan="3" class="muted">Нет активности ключей</td></tr>`;
    }
    for (const row of m.top_keys || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td></td><td></td><td></td>`;
      tr.children[0].textContent = `${row.name} (${row.key_prefix || "?"}…)`;
      tr.children[1].textContent = row.requests.toLocaleString("ru");
      tr.children[2].textContent = row.downloads.toLocaleString("ru");
      keyBody.appendChild(tr);
    }
    const cat = m.catalog || {};
    fillStatsGrid($("catalog-metrics"), [
      ["Моделей", (cat.total || 0).toLocaleString("ru")],
      ["С описанием", (cat.described || 0).toLocaleString("ru")],
      ["В Qdrant", (cat.embedded || 0).toLocaleString("ru")],
      ["Ждут превью", (m.pending_previews || 0).toLocaleString("ru")],
      ["Объём", formatSize(cat.total_bytes || 0)],
    ]);
    const bars = $("category-bars");
    bars.innerHTML = "";
    const cats = cat.categories || [];
    const catMax = Math.max(1, ...cats.map((x) => x.count));
    for (const row of cats.slice(0, 12)) {
      const line = document.createElement("div");
      line.className = "bar-row";
      const name = document.createElement("span");
      name.textContent = row.category;
      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = "bar-fill";
      fill.style.width = `${(row.count / catMax) * 100}%`;
      track.appendChild(fill);
      const n = document.createElement("span");
      n.className = "muted";
      n.textContent = String(row.count);
      line.appendChild(name);
      line.appendChild(track);
      line.appendChild(n);
      bars.appendChild(line);
    }
  } catch (e) {
    $("metrics-stats").textContent = `Не удалось загрузить метрики: ${e.message}`;
  }
}

let modelsOffset = 0;
const MODELS_PAGE = 40;

async function loadModels() {
  const err = $("models-error");
  err.hidden = true;
  const q = $("model-q").value.trim();
  const category = $("model-category").value;
  const params = new URLSearchParams({ limit: String(MODELS_PAGE), offset: String(modelsOffset) });
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  try {
    const res = await fetch(`/api/admin/models?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    $("models-total").textContent = `Найдено: ${data.total.toLocaleString("ru")}`;
    const tbody = $("models-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">Ничего не найдено</td></tr>`;
    }
    for (const item of data.items) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td>`;
      tr.children[0].textContent = item.name;
      tr.children[0].title = item.path;
      tr.children[1].textContent = item.category || "—";
      tr.children[2].textContent = item.ext || "—";
      tr.children[3].textContent = formatSize(item.size);
      tr.children[4].textContent = item.preview_status || "—";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost";
      del.textContent = "Удалить";
      del.addEventListener("click", () => deleteModel(item.path, item.name));
      tr.children[5].appendChild(del);
      tbody.appendChild(tr);
    }
    $("models-prev").disabled = modelsOffset <= 0;
    $("models-next").disabled = modelsOffset + MODELS_PAGE >= data.total;
  } catch (e) {
    err.hidden = false;
    err.textContent = e.message;
  }
}

async function deleteModel(path, name) {
  if (!confirm(`Убрать «${name}» из каталога?`)) return;
  const res = await fetch(`/api/admin/model?path=${encodeURIComponent(path)}`, { method: "DELETE" });
  if (!res.ok) {
    $("models-error").hidden = false;
    $("models-error").textContent = `Не удалось удалить (HTTP ${res.status})`;
    return;
  }
  loadModels();
  loadStats();
  loadMetrics();
}

$("model-search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  modelsOffset = 0;
  loadModels();
});
$("models-prev").addEventListener("click", () => {
  modelsOffset = Math.max(0, modelsOffset - MODELS_PAGE);
  loadModels();
});
$("models-next").addEventListener("click", () => {
  modelsOffset += MODELS_PAGE;
  loadModels();
});

