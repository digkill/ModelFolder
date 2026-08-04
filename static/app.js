import {
  UNIT_ACTIONS,
  defaultUnitMain,
  applyUnitTransform,
  serializeUnitMain,
} from "./unit_main.js";

const EXT_ICON = {
  glb: "📦",
  gltf: "📐",
  fbx: "🎬",
  usdz: "🍎",
  flb: "📁",
};

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileUrl(path) {
  const sep = "::";
  const idx = path.indexOf(sep);
  if (idx === -1) {
    const enc = path.split("/").map(encodeURIComponent).join("/");
    return `${window.location.origin}/files/${enc}`;
  }
  const left = path.slice(0, idx);
  const right = path.slice(idx + sep.length);
  const disk = left.split("/").map(encodeURIComponent).join("/");
  const inner = right.split("/").map(encodeURIComponent).join("/");
  return `${window.location.origin}/files/${disk}::${inner}`;
}

function apiDownloadUrl(path) {
  return `/api/file?path=${encodeURIComponent(path)}`;
}

let items = [];
let threeCleanup = null;
let unitMainState = defaultUnitMain();
let unitPanelAbort = null;

function disposeUnitPanel() {
  if (unitPanelAbort) {
    unitPanelAbort.abort();
    unitPanelAbort = null;
  }
}

function initUnitPanelStructure() {
  const tf = document.getElementById("unit-transform");
  if (tf.dataset.ready) return;
  tf.dataset.ready = "1";
  const defs = [
    ["P", ["px", "py", "pz"]],
    ["S", ["sx", "sy", "sz"]],
    ["R°", ["rx", "ry", "rz"]],
  ];
  for (const [label, ids] of defs) {
    const row = document.createElement("div");
    row.className = "unit-tf-row";
    const lab = document.createElement("span");
    lab.className = "unit-tf-label";
    lab.textContent = label;
    row.appendChild(lab);
    for (const id of ids) {
      const inp = document.createElement("input");
      inp.type = "number";
      inp.className = "unit-num";
      inp.step = "any";
      inp.id = `unit-${id}`;
      row.appendChild(inp);
    }
    tf.appendChild(row);
  }
  const map = document.getElementById("unit-action-map");
  for (const action of UNIT_ACTIONS) {
    const row = document.createElement("div");
    row.className = "unit-action-row";
    const lab = document.createElement("label");
    lab.htmlFor = `unit-act-${action}`;
    lab.textContent = action;
    const sel = document.createElement("select");
    sel.id = `unit-act-${action}`;
    sel.dataset.action = action;
    sel.className = "unit-select-action";
    row.appendChild(lab);
    row.appendChild(sel);
    map.appendChild(row);
  }
}

function readTransformFromInputs() {
  const n = (id, scaleAxis = false) => {
    const el = document.getElementById(`unit-${id}`);
    const v = parseFloat(el?.value);
    if (scaleAxis) {
      return Number.isFinite(v) && v !== 0 ? v : 1;
    }
    return Number.isFinite(v) ? v : 0;
  };
  return {
    position: [n("px"), n("py"), n("pz")],
    scale: [n("sx", true), n("sy", true), n("sz", true)],
    rotation: [n("rx"), n("ry"), n("rz")],
  };
}

function writeTransformToInputs(u) {
  const w = (id, v) => {
    const el = document.getElementById(`unit-${id}`);
    if (el) el.value = String(v);
  };
  w("px", u.position[0]);
  w("py", u.position[1]);
  w("pz", u.position[2]);
  w("sx", u.scale[0]);
  w("sy", u.scale[1]);
  w("sz", u.scale[2]);
  w("rx", u.rotation[0]);
  w("ry", u.rotation[1]);
  w("rz", u.rotation[2]);
}

function fillClipSelect(select, clips) {
  const keep = select.value;
  select.innerHTML = "";
  const o0 = document.createElement("option");
  o0.value = "";
  o0.textContent = "—";
  select.appendChild(o0);
  for (const c of clips) {
    const o = document.createElement("option");
    o.value = c;
    o.textContent = c;
    select.appendChild(o);
  }
  if (clips.includes(keep)) select.value = keep;
}

function fillAllClipSelects(clips) {
  fillClipSelect(document.getElementById("unit-clip-preview"), clips);
  for (const action of UNIT_ACTIONS) {
    fillClipSelect(document.getElementById(`unit-act-${action}`), clips);
  }
}

function collectUnitMain() {
  Object.assign(unitMainState, readTransformFromInputs());
  for (const action of UNIT_ACTIONS) {
    const sel = document.getElementById(`unit-act-${action}`);
    unitMainState.animations[action] = sel?.value || null;
  }
  return unitMainState;
}

/**
 * @param {string[]} clips
 * @param {(name: string) => void} playClip
 * @param {() => import('three').Object3D | null | undefined} rootGet
 */
function wireUnitPanel(clips, playClip, rootGet) {
  disposeUnitPanel();
  initUnitPanelStructure();
  unitMainState = defaultUnitMain();
  writeTransformToInputs(unitMainState);
  fillAllClipSelects(clips);

  const abort = new AbortController();
  unitPanelAbort = abort;
  const sig = { signal: abort.signal };

  const applyTransform = () => {
    Object.assign(unitMainState, readTransformFromInputs());
    const root = rootGet();
    if (root) applyUnitTransform(root, unitMainState);
  };

  for (const id of ["px", "py", "pz", "sx", "sy", "sz", "rx", "ry", "rz"]) {
    document.getElementById(`unit-${id}`).addEventListener("input", applyTransform, sig);
  }

  document.getElementById("unit-clip-preview").addEventListener(
    "change",
    (e) => {
      playClip(e.target.value);
    },
    sig,
  );

  for (const action of UNIT_ACTIONS) {
    document.getElementById(`unit-act-${action}`).addEventListener(
      "change",
      (e) => {
        unitMainState.animations[action] = e.target.value || null;
      },
      sig,
    );
  }

  document.getElementById("unit-panel").hidden = false;
  applyTransform();
}

function setStatus(text, isError = false) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.classList.toggle("error", isError);
  el.hidden = false;
}

function hideStatus() {
  document.getElementById("status").hidden = true;
}

function formatScanMeta(lastScan, pending) {
  if (lastScan == null) return "";
  const d = new Date(lastScan * 1000);
  const t = d.toLocaleString();
  const p = pending > 0 ? ` · превью в очереди: ${pending}` : "";
  return `Каталог: обновлён ${t}${p}`;
}

function contentBadges(content) {
  if (!content || content.checked_at == null) return [];
  const out = [
    {
      text: content.adult ? "18+" : "не 18+",
      danger: Boolean(content.adult),
      title: content.adult ? "ИИ: есть 18+/нагота" : "ИИ: 18+/нагота не обнаружены",
    },
  ];
  if (content.violence) out.push({ text: "насилие", danger: true, title: "ИИ: есть насилие" });
  if (content.horror) out.push({ text: "ужасы", danger: true, title: "ИИ: есть ужасы" });
  if (content.gore) out.push({ text: "кровь", danger: true, title: "ИИ: есть кровь/травмы" });
  return out;
}

function buildModelsQuery() {
  const p = new URLSearchParams();
  const raw = document.getElementById("filter-tags").value.trim();
  if (raw) {
    if (document.getElementById("filter-tags-all").checked) {
      p.set("tags_all", raw);
    } else {
      p.set("tags", raw);
    }
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function loadModels() {
  setStatus("Загрузка…");
  document.getElementById("grid").hidden = true;
  try {
    const q = buildModelsQuery();
    const [modelsRes, statusRes] = await Promise.all([
      fetch(`/api/models${q}`),
      fetch("/api/status"),
    ]);
    if (!modelsRes.ok) throw new Error(`HTTP ${modelsRes.status}`);
    const data = await modelsRes.json();
    items = data.items || [];
    let rootLine = data.root || "";
    if (statusRes.ok) {
      const st = await statusRes.json();
      const extra = formatScanMeta(st.last_full_scan_at, st.pending_previews);
      if (extra) rootLine = `${rootLine}\n${extra}`;
    }
    document.getElementById("root-path").textContent = rootLine;
    renderGrid();
    hideStatus();
    document.getElementById("grid").hidden = items.length === 0;
    if (items.length === 0) {
      setStatus("В папке моделей нет файлов с расширениями .fbx .glb .gltf .usdz .flb");
    }
  } catch (e) {
    setStatus(String(e.message || e), true);
  }
}

function renderGrid() {
  const grid = document.getElementById("grid");
  const q = document.getElementById("filter").value.trim().toLowerCase();
  grid.innerHTML = "";
  const filtered = q
    ? items.filter((it) => {
        const hay = `${it.name} ${it.path}`.toLowerCase();
        if (hay.includes(q)) return true;
        const tl = it.tag_list || [];
        return tl.some((t) => String(t).toLowerCase().includes(q));
      })
    : items;

  for (const it of filtered) {
    const icon = EXT_ICON[it.ext] || "📄";
    const card = document.createElement("article");
    card.className = "card";
    const preview = document.createElement("div");
    preview.className = "card-preview";
    preview.setAttribute("aria-hidden", "true");

    if (it.preview_url) {
      preview.classList.add("has-image");
      const img = document.createElement("img");
      img.alt = "";
      img.loading = "lazy";
      img.src = it.preview_url;
      img.addEventListener("error", () => {
        preview.classList.remove("has-image");
      });
      preview.appendChild(img);
    }

    const fallback = document.createElement("span");
    fallback.className = "emoji-fallback";
    fallback.textContent = icon;
    preview.appendChild(fallback);

    if (it.preview_status === "pending") {
      const badge = document.createElement("span");
      badge.className = "pending-badge";
      badge.textContent = "Превью…";
      preview.appendChild(badge);
    }

    const body = document.createElement("div");
    body.className = "card-body";
    body.innerHTML = `
      <h3 class="card-title"></h3>
      <p class="card-path"></p>
      <p class="card-desc"></p>
      <div class="card-meta">
        <div class="card-meta-primary">
          <span class="badge"></span>
          <div class="card-tags"></div>
        </div>
        <span class="size"></span>
      </div>
    `;
    body.querySelector(".card-title").textContent = it.name;
    body.querySelector(".card-path").textContent = it.path;
    body.querySelector(".badge").textContent = it.ext;
    body.querySelector(".size").textContent = formatSize(it.size);

    const descEl = body.querySelector(".card-desc");
    if (it.description) {
      descEl.textContent = it.description;
      descEl.title = it.description;
    } else {
      descEl.remove();
    }

    const tagsWrap = body.querySelector(".card-tags");
    tagsWrap.addEventListener("click", (e) => e.stopPropagation());
    const tagRows = it.tags && it.tags.length ? it.tags : (it.tag_list || []).map((tag) => ({ tag, source: "openai" }));
    for (const t of tagRows) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = `tag-chip ${t.source === "manual" ? "manual" : "openai"}`;
      chip.textContent = t.tag;
      chip.title =
        (t.source === "manual" ? "Ручной тег" : "Тег ИИ") + " — нажмите, чтобы отфильтровать";
      chip.setAttribute("aria-label", `Фильтр по тегу «${t.tag}»`);
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        document.getElementById("filter-tags").value = t.tag;
        document.getElementById("filter-tags-all").checked = false;
        loadModels();
      });
      tagsWrap.appendChild(chip);
    }

    for (const b of contentBadges(it.content)) {
      const chip = document.createElement("span");
      chip.className = `content-chip ${b.danger ? "danger" : "ok"}`;
      chip.textContent = b.text;
      chip.title = b.title;
      tagsWrap.appendChild(chip);
    }

    if (it.has_blend && it.blend_download_url) {
      const br = document.createElement("div");
      br.className = "blend-row";
      br.addEventListener("click", (e) => e.stopPropagation());
      const a = document.createElement("a");
      a.href = it.blend_download_url;
      a.className = "blend-link";
      const bn =
        (it.blend_path && it.blend_path.split("/").pop()) ||
        (it.blend_path && it.blend_path.split("::").pop()) ||
        "source.blend";
      a.textContent = `Blender: ${bn}`;
      a.setAttribute("download", "");
      br.appendChild(a);
      body.appendChild(br);
    }

    card.appendChild(preview);
    card.appendChild(body);
    card.style.cursor = "pointer";
    card.addEventListener("click", () => openViewer(it));
    grid.appendChild(card);
  }

  if (filtered.length === 0 && items.length > 0) {
    setStatus("Ничего не найдено по фильтру");
    grid.hidden = true;
  } else if (filtered.length > 0) {
    hideStatus();
    grid.hidden = false;
  }
}

function disposeThree() {
  disposeUnitPanel();
  if (typeof threeCleanup === "function") {
    threeCleanup();
    threeCleanup = null;
  }
  const container = document.getElementById("three-container");
  while (container.firstChild) container.removeChild(container.firstChild);
}

async function openViewer(it) {
  const overlay = document.getElementById("overlay");
  const mv = document.getElementById("mv");
  const threeContainer = document.getElementById("three-container");
  const fallback = document.getElementById("viewer-fallback");
  const title = document.getElementById("modal-title");
  const dl = document.getElementById("download-link");

  disposeThree();
  document.getElementById("unit-panel").hidden = true;

  fallback.classList.add("hidden");
  fallback.textContent = "";
  mv.style.display = "none";
  threeContainer.style.display = "none";

  title.textContent = it.name;
  dl.href = apiDownloadUrl(it.path);
  dl.download = it.name;

  const url = fileUrl(it.path);
  const ext = it.ext.toLowerCase();

  overlay.classList.add("open");

  if (ext === "glb" || ext === "gltf") {
    mv.style.display = "block";
    mv.src = url;
    const connect = () => {
      const clips = mv.availableAnimations || [];
      wireUnitPanel(
        clips,
        (name) => {
          mv.animationName = name || "";
          if (name && typeof mv.play === "function") mv.play();
        },
        () => mv.model,
      );
      const prev = document.getElementById("unit-clip-preview");
      if (clips.length && prev) {
        const pick = clips.find((c) => /idle/i.test(c)) || clips[0];
        prev.value = pick;
        mv.animationName = pick;
        if (typeof mv.play === "function") mv.play();
      }
    };
    if (mv.loaded) connect();
    else mv.addEventListener("load", connect, { once: true });
    return;
  }

  if (ext === "usdz") {
    mv.style.display = "block";
    mv.src = url;
    fallback.classList.remove("hidden");
    fallback.textContent =
      "USDZ лучше всего смотрится в Safari / на iOS (AR). В других браузерах предпросмотр может не работать — используйте «Скачать».";
    return;
  }

  if (ext === "fbx") {
    threeContainer.style.display = "block";
    try {
      await runFbxViewer(threeContainer, url);
    } catch (e) {
      fallback.classList.remove("hidden");
      fallback.textContent = `Не удалось загрузить FBX: ${e.message || e}. Скачайте файл и откройте в DCC.`;
    }
    return;
  }

  fallback.classList.remove("hidden");
  fallback.textContent =
    "Предпросмотр для этого формата не настроен. Нажмите «Скачать», чтобы сохранить файл.";
}

function closeModal() {
  document.getElementById("overlay").classList.remove("open");
  const mv = document.getElementById("mv");
  mv.src = "";
  document.getElementById("unit-panel").hidden = true;
  disposeThree();
}

async function runFbxViewer(container, url) {
  const THREE = await import("three");
  const { FBXLoader } = await import("three/addons/loaders/FBXLoader.js");
  const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");

  const width = container.clientWidth || 640;
  const height = container.clientHeight || 360;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0c0f);

  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 5000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  container.appendChild(renderer.domElement);

  const hemi = new THREE.HemisphereLight(0xffffff, 0x444444, 1);
  scene.add(hemi);
  const dir = new THREE.DirectionalLight(0xffffff, 1.2);
  dir.position.set(3, 8, 5);
  scene.add(dir);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  const loader = new FBXLoader();
  const group = await new Promise((resolve, reject) => {
    loader.load(url, resolve, undefined, reject);
  });

  scene.add(group);

  const mixer = new THREE.AnimationMixer(group);
  const clock = new THREE.Clock();
  let currentAction = null;

  function playClip(name) {
    if (!name) {
      if (currentAction) {
        currentAction.fadeOut(0.15);
        currentAction = null;
      }
      return;
    }
    const clip = group.animations.find((c) => c.name === name);
    if (!clip) return;
    if (currentAction) currentAction.fadeOut(0.15);
    const next = mixer.clipAction(clip);
    currentAction = next;
    next.reset().fadeIn(0.15).play();
  }

  const clipNames = group.animations.map((c) => c.name);

  const box = new THREE.Box3().setFromObject(group);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 1);
  const dist = maxDim * 1.8;
  camera.position.set(center.x + dist * 0.6, center.y + dist * 0.4, center.z + dist * 0.6);
  camera.lookAt(center);
  controls.target.copy(center);
  controls.update();

  wireUnitPanel(clipNames, playClip, () => group);

  const prev = document.getElementById("unit-clip-preview");
  if (clipNames.length && prev) {
    const pick = clipNames.find((c) => /idle/i.test(c)) || clipNames[0];
    prev.value = pick;
    playClip(pick);
  }

  let raf = 0;
  function animate() {
    raf = requestAnimationFrame(animate);
    mixer.update(clock.getDelta());
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  const ro = new ResizeObserver(() => {
    const w = container.clientWidth;
    const h = container.clientHeight || 360;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  });
  ro.observe(container);

  threeCleanup = () => {
    cancelAnimationFrame(raf);
    mixer.stopAllAction();
    ro.disconnect();
    controls.dispose();
    renderer.dispose();
    if (renderer.domElement.parentNode === container) {
      container.removeChild(renderer.domElement);
    }
    scene.traverse((obj) => {
      if (obj.isMesh) {
        obj.geometry?.dispose();
        const mat = obj.material;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose?.());
        else mat?.dispose?.();
      }
    });
  };
}

document.getElementById("unit-copy-json").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(serializeUnitMain(collectUnitMain()));
  } catch (_) {
    /* ignore */
  }
});

document.getElementById("reload").addEventListener("click", async () => {
  try {
    await fetch("/api/scan", { method: "POST" });
  } catch (_) {
    /* ignore */
  }
  await loadModels();
});
document.getElementById("tag-ai").addEventListener("click", async () => {
  const btn = document.getElementById("tag-ai");
  btn.disabled = true;
  try {
    const r = await fetch("/api/tags/auto?limit=40&only_missing=true&sync=true", {
      method: "POST",
    });
    let j = {};
    try {
      j = await r.json();
    } catch (_) {
      /* ignore */
    }
    if (!r.ok) {
      const d = j.detail;
      setStatus(typeof d === "string" ? d : JSON.stringify(d || j), true);
      return;
    }
    if (j.ok === false && j.error) {
      setStatus(j.error, true);
      return;
    }
    const errN = j.error_count != null ? `, ошибок: ${j.error_count}` : "";
    setStatus(`ИИ-теги: обработано ${j.processed ?? 0} (кандидатов: ${j.candidates ?? "—"})${errN}`);
    await loadModels();
  } catch (e) {
    setStatus(String(e.message || e), true);
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("filter-tags").addEventListener("change", () => loadModels());
document.getElementById("filter-tags").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadModels();
});
document.getElementById("filter-tags-all").addEventListener("change", () => loadModels());
document.getElementById("filter").addEventListener("input", renderGrid);
document.getElementById("close-modal").addEventListener("click", closeModal);
document.getElementById("overlay").addEventListener("click", (e) => {
  if (e.target.id === "overlay") closeModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

loadModels();
