const bridge = window.AstrBotPluginPage;
const assetToken =
  new URLSearchParams(window.location.search).get("asset_token") || "";

const PRESET_TAGS = [
  "高兴",
  "开心",
  "害羞",
  "难过",
  "伤心",
  "生气",
  "着急",
  "惊讶",
  "疑惑",
  "期待",
  "平静",
  "得意",
  "委屈",
  "无奈",
];

const vn = document.getElementById("vn");
const vnBg = document.getElementById("vn-bg");
const vnSprite = document.getElementById("vn-sprite");
const vnSpriteBack = document.getElementById("vn-sprite-back");
const tagName = document.getElementById("tag-name");
const tagEmotion = document.getElementById("tag-emotion");
const tagAffection = document.getElementById("tag-affection");
const choicesBox = document.getElementById("vn-choices");
const dialogue = document.getElementById("vn-dialogue");
const speaker = document.getElementById("speaker");
const lineBox = document.getElementById("line");
const nextHint = document.getElementById("next-hint");
const emptyBox = document.getElementById("vn-empty");
const statusBox = document.getElementById("vn-status");
const composer = document.getElementById("vn-composer");
const composerToggle = document.getElementById("composer-toggle");
const manualInput = document.getElementById("manual-input");
const manualSend = document.getElementById("manual-send");
const affectLayer = document.getElementById("affect-layer");
const logWindow = document.getElementById("log-window");
const logList = document.getElementById("log-list");
const openLogButton = document.getElementById("open-log");
const closeLogButton = document.getElementById("close-log");

const drawer = document.getElementById("drawer");
const drawerScrim = document.getElementById("drawer-scrim");
const openDrawerButton = document.getElementById("open-drawer");
const closeDrawerButton = document.getElementById("close-drawer");

const tabButtons = {
  friends: document.getElementById("tab-btn-friends"),
  persona: document.getElementById("tab-btn-persona"),
  characters: document.getElementById("tab-btn-characters"),
  backgrounds: document.getElementById("tab-btn-backgrounds"),
};
const tabPanels = {
  friends: document.getElementById("panel-friends"),
  persona: document.getElementById("panel-persona"),
  characters: document.getElementById("panel-characters"),
  backgrounds: document.getElementById("panel-backgrounds"),
};

const rosterHint = document.getElementById("roster-hint");
const setupGuide = document.getElementById("setup-guide");
const guideCharacter = document.getElementById("guide-character");
const guideBackground = document.getElementById("guide-background");
const guideBind = document.getElementById("guide-bind");
const bindingList = document.getElementById("binding-list");
const sessionList = document.getElementById("session-list");
const bindingEditor = document.getElementById("binding-editor");
const bindingTitle = document.getElementById("binding-title");
const bindCharacter = document.getElementById("bind-character");
const bindBackground = document.getElementById("bind-background");
const relationInput = document.getElementById("relation-input");
const saveRelationButton = document.getElementById("save-relation");
const selfPersonaInput = document.getElementById("self-persona-input");
const saveSelfPersonaButton = document.getElementById("save-self-persona");
const unbindButton = document.getElementById("unbind-button");

const newCharacterName = document.getElementById("new-character-name");
const newCharacterButton = document.getElementById("new-character");
const loadDefaultsButton = document.getElementById("load-defaults");
const characterList = document.getElementById("character-list");
const characterEditor = document.getElementById("character-editor");
const characterTitle = document.getElementById("character-title");
const spriteUpload = document.getElementById("sprite-upload");
const spriteList = document.getElementById("sprite-list");
const backgroundUpload = document.getElementById("background-upload");
const backgroundList = document.getElementById("background-list");

const state = {
  umo: "",
  binding: null,
  character: null,
  background: null,
  tag: "",
  messages: [],
  options: [],
  cursor: -1,
  characters: [],
  backgrounds: [],
  editingCharacterId: 0,
  armed: {},
  bindingCount: 0,
  typing: null,
  statusTimer: null,
  spriteTimer: null,
  pendingEntrance: false,
  localEditAt: 0,
  unbindArmed: false,
};

/* ------------------------------ small helpers ------------------------------ */

function assetUrl(name) {
  if (!name) return "";
  const suffix = assetToken ? `?asset_token=${encodeURIComponent(assetToken)}` : "";
  return `./assets/${encodeURIComponent(name)}${suffix}`;
}

function friendLabel() {
  return state.binding?.friend_name || "好友";
}

function setStatus(text, isError = false, autoHide = false) {
  if (state.statusTimer !== null) {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = null;
  }
  statusBox.classList.remove("thinking");
  if (!text) {
    statusBox.hidden = true;
    statusBox.textContent = "";
    statusBox.classList.remove("error");
    return;
  }
  statusBox.hidden = false;
  statusBox.textContent = text;
  statusBox.classList.toggle("error", isError);
  if (autoHide) {
    state.statusTimer = window.setTimeout(() => setStatus(""), 2800);
  }
}

/**
 * Show the waiting indicator; the trailing dots are animated by CSS.
 *
 * @param {string} text label shown before the dots
 */
function setThinking(text = "思考中") {
  setStatus(text);
  statusBox.classList.add("thinking");
}

function clearTyping() {
  if (state.typing === null) return;
  window.clearInterval(state.typing);
  state.typing = null;
}

function markLocalEdit() {
  state.localEditAt = Date.now();
}

/**
 * Two-step inline confirmation. The plugin page runs in a sandboxed iframe
 * without allow-modals, so window.confirm() is silently blocked and must not
 * be used.
 */
function armOrRun(key, rerender, run) {
  if (state.armed[key]) {
    delete state.armed[key];
    void run();
    return;
  }
  state.armed = { [key]: true };
  rerender();
}

/* ------------------------------ dialogue ------------------------------ */

function updateNextHint() {
  nextHint.hidden = state.cursor >= state.messages.length - 1;
}

function showLine(index, animate) {
  const message = state.messages[index];
  if (!message) {
    dialogue.hidden = true;
    return;
  }
  dialogue.hidden = false;
  speaker.textContent = message.role === "me" ? "我" : friendLabel();
  clearTyping();
  lineBox.classList.remove("typing");
  if (animate) {
    lineBox.textContent = "";
    lineBox.classList.add("typing");
    let shown = 0;
    state.typing = window.setInterval(() => {
      shown += 1;
      lineBox.textContent = message.text.slice(0, shown);
      if (shown >= message.text.length) {
        clearTyping();
        lineBox.classList.remove("typing");
      }
    }, 26);
  } else {
    lineBox.textContent = message.text;
  }
  updateNextHint();
}

function advance() {
  if (state.cursor < state.messages.length - 1) {
    state.cursor += 1;
    showLine(state.cursor, false);
  }
}

function pushMessage(role, text, animate) {
  state.messages.push({ role, text });
  state.cursor = state.messages.length - 1;
  showLine(state.cursor, animate);
}

/* ------------------------------ stage ------------------------------ */

function pickSprite(tag) {
  const sprites = state.character?.sprites || [];
  if (sprites.length === 0) return "";
  const wanted = (tag || "").trim().toLowerCase();
  if (wanted) {
    const hit = sprites.find((sprite) =>
      (sprite.tags || []).some((item) => item.toLowerCase() === wanted),
    );
    if (hit) return hit.file;
  }
  const fallback = sprites.find((sprite) => sprite.is_default) || sprites[0];
  return fallback.file;
}

/**
 * Apply a portrait file.
 *
 * @param {string} file asset file name, empty clears the portrait
 * @param {boolean} entrance true for the first appearance of a character
 *   (fade + slide in), false for an emotion change (cross-fade only)
 */
function applySprite(file, entrance = false) {
  const next = file ? `url("${assetUrl(file)}")` : "";
  window.clearTimeout(state.spriteTimer);

  if (entrance) {
    vnSpriteBack.classList.remove("show");
    vnSprite.classList.remove("enter");
    vnSprite.classList.remove("switch");
    vnSprite.style.cssText = "";
    vnSprite.style.backgroundImage = next;
    void vnSprite.offsetWidth;
    if (next) vnSprite.classList.add("enter");
    return;
  }
  if (!next) {
    vnSpriteBack.classList.remove("show");
    vnSprite.classList.remove("enter");
    vnSprite.style.cssText = "";
    vnSprite.style.backgroundImage = "";
    return;
  }
  if (vnSprite.style.backgroundImage === next) return;

  // Cross-fade: park the outgoing portrait behind, fade the new one in.
  vnSpriteBack.style.backgroundImage = vnSprite.style.backgroundImage || next;
  vnSpriteBack.classList.add("show");
  vnSprite.classList.add("switch");
  vnSprite.style.backgroundImage = next;
  vnSprite.style.transition = "opacity 0.26s ease";
  vnSprite.style.opacity = "0";
  void vnSprite.offsetWidth;
  vnSprite.style.opacity = "1";
  state.spriteTimer = window.setTimeout(() => {
    vnSpriteBack.classList.remove("show");
    vnSprite.style.transition = "";
    vnSprite.style.opacity = "";
  }, 340);
}

function renderStage() {
  const binding = state.binding;
  emptyBox.hidden = Boolean(binding);
  if (!binding) {
    vnBg.style.backgroundImage = "";
    applySprite("");
    tagName.hidden = true;
    tagEmotion.hidden = true;
    tagAffection.hidden = true;
    dialogue.hidden = true;
    bindingEditor.hidden = true;
    return;
  }
  tagName.hidden = false;
  tagName.textContent = binding.friend_name || binding.umo;
  tagEmotion.hidden = !state.tag;
  tagEmotion.textContent = state.tag ? `情绪 ${state.tag}` : "";
  tagAffection.hidden = false;
  const affection = Math.max(0, Math.min(100, Number(binding.affection) || 0));
  tagAffection.textContent = `❤ ${affection} / 100`;
  tagAffection.style.setProperty("--aff", `${affection}%`);
  vnBg.style.backgroundImage = state.background?.file
    ? `url("${assetUrl(state.background.file)}")`
    : "";
  applySprite(pickSprite(state.tag), state.pendingEntrance);
  state.pendingEntrance = false;
  renderBindingEditor();
}

/* ------------------------------ choices ------------------------------ */

function clearChoices() {
  state.options = [];
  choicesBox.hidden = true;
  choicesBox.replaceChildren();
  vn.classList.remove("choosing");
}

function renderChoices(options) {
  state.options = options || [];
  choicesBox.replaceChildren();
  if (state.options.length === 0) {
    choicesBox.hidden = true;
    vn.classList.remove("choosing");
    return;
  }
  const composerOpen = !composer.hidden;
  vn.classList.toggle("choosing", !composerOpen);
  state.options.forEach((option, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice";

    const num = document.createElement("span");
    num.className = "num";
    num.textContent = `${index + 1}.`;

    const body = document.createElement("span");
    body.className = "choice-body";
    const text = document.createElement("span");
    text.className = "choice-text";
    text.textContent = option.text;
    body.append(text);

    const meta = document.createElement("span");
    meta.className = "choice-meta";
    if (option.angle) {
      const angle = document.createElement("span");
      angle.className = "choice-angle";
      angle.textContent = option.angle;
      meta.append(angle);
    }
    const delta = Number(option.affection) || 0;
    if (delta !== 0) {
      const chip = document.createElement("span");
      chip.className = `choice-delta ${delta > 0 ? "up" : "down"}`;
      chip.textContent = `${delta > 0 ? "+" : ""}${delta}`;
      meta.append(chip);
    }
    if (meta.childNodes.length) body.append(meta);

    button.append(num, body);
    if (option.used) {
      button.classList.add("used");
      button.disabled = true;
    }
    button.addEventListener("click", () =>
      void sendText(option.text, option.id, button),
    );
    choicesBox.append(button);
  });
  const again = document.createElement("button");
  again.type = "button";
  again.className = "vn-regenerate";
  again.textContent = "换一批候选";
  again.addEventListener("click", () => void regenerate());
  choicesBox.append(again);
  // Keep the floating composer and the option list from overlapping.
  choicesBox.hidden = composerOpen;
}

/* ------------------------------ affection effect ------------------------------ */

function showAffectionEffect(delta, value, angle) {
  if (!delta) return;
  const floating = document.createElement("div");
  floating.className = `affect ${delta > 0 ? "up" : "down"}`;
  const amount = document.createElement("span");
  amount.className = "affect-amount";
  amount.textContent = `${delta > 0 ? "+" : ""}${delta}`;
  floating.append(amount);
  if (angle) {
    const label = document.createElement("span");
    label.className = "affect-angle";
    label.textContent = angle;
    floating.append(label);
  }
  affectLayer.append(floating);
  window.setTimeout(() => floating.remove(), 1800);

  if (value !== undefined && state.binding) {
    state.binding.affection = value;
  }
  tagAffection.classList.remove("pop");
  void tagAffection.offsetWidth;
  tagAffection.classList.add("pop");
  renderStage();
}

/* ------------------------------ chat log window ------------------------------ */

function renderLogWindow(messages) {
  logList.replaceChildren();
  if (messages.length === 0) {
    const item = document.createElement("li");
    item.className = "log-empty";
    item.textContent = "还没有聊天记录";
    logList.append(item);
    return;
  }
  for (const message of messages) {
    const item = document.createElement("li");
    item.className = message.role === "me" ? "log-line me" : "log-line";
    const who = document.createElement("span");
    who.className = "log-who";
    who.textContent = message.role === "me" ? "我" : friendLabel();
    const text = document.createElement("span");
    text.className = "log-text";
    text.textContent = message.text;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = message.created_at
      ? new Date(message.created_at * 1000).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";
    item.append(who, text, time);
    logList.append(item);
  }
  logList.scrollTop = logList.scrollHeight;
}

async function openLog() {
  if (!state.umo) {
    setStatus("先选一位好友", true);
    return;
  }
  setComposerOpen(false);
  try {
    const data = await bridge.apiGet("log", { umo: state.umo, limit: 400 });
    renderLogWindow(data.messages || []);
    logWindow.hidden = false;
  } catch (error) {
    setStatus(`读取记录失败：${error.message}`, true);
  }
}

function setComposerOpen(open) {
  composer.hidden = !open;
  composerToggle.classList.toggle("active", open);
  if (open) {
    logWindow.hidden = true;
    // The floating composer would sit on top of the option list.
    if (state.options.length) {
      choicesBox.hidden = true;
      vn.classList.remove("choosing");
    }
    manualInput.focus();
    return;
  }
  if (state.options.length) {
    choicesBox.hidden = false;
    vn.classList.add("choosing");
  }
}

/* ------------------------------ tabs & drawer ------------------------------ */

function setTab(name) {
  for (const [key, button] of Object.entries(tabButtons)) {
    button.classList.toggle("active", key === name);
    tabPanels[key].hidden = key !== name;
  }
}

function setDrawerOpen(open) {
  drawer.hidden = !open;
  drawerScrim.hidden = !open;
}

/* ------------------------------ friends panel ------------------------------ */

function emptyItem(text) {
  const item = document.createElement("li");
  item.className = "empty";
  item.textContent = text;
  return item;
}

function rosterItem(name, note, umo) {
  const item = document.createElement("li");
  if (umo) item.dataset.umo = umo;
  const nameNode = document.createElement("span");
  nameNode.className = "item-name";
  nameNode.textContent = name;
  item.append(nameNode);
  if (note) {
    const noteNode = document.createElement("span");
    noteNode.className = "item-note";
    noteNode.textContent = note;
    item.append(noteNode);
  }
  return item;
}

function renderGuide() {
  const steps = [
    [guideCharacter, state.characters.length > 0],
    [guideBackground, state.backgrounds.length > 0],
    [guideBind, state.bindingCount > 0],
  ];
  for (const [node, done] of steps) {
    node.classList.toggle("done", done);
  }
  // Hide the checklist once everything is in place.
  setupGuide.hidden = steps.every(([, done]) => done);
}

function renderFriends(data) {
  const bindings = data.bindings || [];
  const sessions = data.sessions || [];
  state.characters = data.characters || [];
  state.backgrounds = data.backgrounds || [];
  state.bindingCount = bindings.length;

  bindingList.replaceChildren();
  sessionList.replaceChildren();

  if (bindings.length === 0) {
    bindingList.append(emptyItem("还没有绑定任何好友"));
  }
  for (const binding of bindings) {
    const item = rosterItem(
      binding.friend_name || binding.umo,
      binding.character_name || "未选角色",
      binding.umo,
    );
    item.classList.toggle("active", binding.umo === state.umo);
    item.addEventListener("click", () => void openScene(binding.umo));
    bindingList.append(item);
  }

  if (sessions.length === 0) {
    sessionList.append(emptyItem("还没有收到过私聊"));
  }
  for (const session of sessions) {
    const item = rosterItem(
      session.friend_name || session.umo,
      session.last_text || "",
    );
    const action = document.createElement("button");
    action.type = "button";
    action.className = "item-action";
    action.textContent = "绑定";
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      void bindSession(session);
    });
    item.append(action);
    sessionList.append(item);
  }

  rosterHint.textContent = bindings.length
    ? `已绑定 ${bindings.length} 位好友`
    : "还没有绑定好友";
  renderGuide();
}

function fillLibrarySelect(select, items, current, emptyLabel, labelOf) {
  select.replaceChildren();
  const none = document.createElement("option");
  none.value = "0";
  none.textContent = emptyLabel;
  select.append(none);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = labelOf(item);
    select.append(option);
  }
  select.value = items.some((item) => String(item.id) === String(current))
    ? String(current)
    : "0";
}

function renderBindingEditor() {
  const binding = state.binding;
  if (!binding) {
    bindingEditor.hidden = true;
    return;
  }
  bindingEditor.hidden = false;
  bindingTitle.textContent = `当前好友：${binding.friend_name || binding.umo}`;
  fillLibrarySelect(
    bindCharacter,
    state.characters,
    binding.character_id,
    "（未选角色，不会切换立绘）",
    (item) => `${item.name}（${item.sprites.length} 张）`,
  );
  fillLibrarySelect(
    bindBackground,
    state.backgrounds,
    binding.background_id,
    "（未选背景）",
    (item) => item.name,
  );
  // Never clobber what the user is currently typing.
  if (document.activeElement !== relationInput) {
    relationInput.value = binding.relation || "";
  }
}

/* ------------------------------ persona settings ------------------------------ */

async function loadSettings() {
  try {
    const data = await bridge.apiGet("settings");
    if (document.activeElement !== selfPersonaInput) {
      selfPersonaInput.value = data.self_persona || "";
    }
  } catch (error) {
    setStatus(`读取人设失败：${error.message}`, true);
  }
}

async function saveSelfPersona() {
  try {
    await bridge.apiPost("settings/save", {
      self_persona: selfPersonaInput.value,
    });
    markLocalEdit();
    setStatus("我的人设已保存，之后生成的候选会按它来写", false, true);
  } catch (error) {
    setStatus(`保存失败：${error.message}`, true);
  }
}

async function saveRelation() {
  if (!state.binding) return;
  try {
    await bridge.apiPost("relation", {
      umo: state.binding.umo,
      relation: relationInput.value,
    });
    markLocalEdit();
    state.binding.relation = relationInput.value;
    setStatus("关系描述已保存", false, true);
  } catch (error) {
    setStatus(`保存失败：${error.message}`, true);
  }
}

async function loadFriends() {
  try {
    renderFriends(await bridge.apiGet("friends"));
    renderBindingEditor();
  } catch (error) {
    rosterHint.textContent = `加载失败：${error.message}`;
  }
}

async function openScene(umo) {
  if (!umo) return;
  state.umo = umo;
  logWindow.hidden = true;
  setComposerOpen(false);
  state.unbindArmed = false;
  unbindButton.classList.remove("armed");
  unbindButton.textContent = "解绑这位好友";
  try {
    const data = await bridge.apiGet("scene", { umo, limit: 80 });
    state.binding = data.binding;
    state.character = data.character;
    state.background = data.background;
    state.tag = data.tag || "";
    state.messages = data.messages || [];
    state.cursor = state.messages.length - 1;
    state.pendingEntrance = true;
    renderStage();
    if (state.cursor >= 0) showLine(state.cursor, false);
    renderChoices(data.options || []);
    setStatus("");
    await loadFriends();
  } catch (error) {
    setStatus(`加载剧情失败：${error.message}`, true);
  }
}

async function applyBinding() {
  const binding = state.binding;
  if (!binding) return;
  try {
    await bridge.apiPost("bind", {
      umo: binding.umo,
      friend_name: binding.friend_name || binding.umo,
      character_id: Number(bindCharacter.value) || 0,
      background_id: Number(bindBackground.value) || 0,
    });
    markLocalEdit();
    await openScene(binding.umo);
  } catch (error) {
    setStatus(`保存失败：${error.message}`, true);
  }
}

async function bindSession(session) {
  if (state.characters.length === 0) {
    setStatus("先去「角色库」新建一个角色，再回来绑定", true);
    setTab("characters");
    return;
  }
  try {
    await bridge.apiPost("bind", {
      umo: session.umo,
      friend_name: session.friend_name || session.friend_id || "好友",
      character_id: state.characters[0].id,
      background_id: state.backgrounds.length ? state.backgrounds[0].id : 0,
    });
    markLocalEdit();
    await openScene(session.umo);
    setDrawerOpen(false);
    setStatus(`已绑定 ${session.friend_name || session.umo}`, false, true);
  } catch (error) {
    setStatus(`绑定失败：${error.message}`, true);
  }
}

/* ------------------------------ character library ------------------------------ */

function renderCharacterList() {
  characterList.replaceChildren();
  if (state.characters.length === 0) {
    characterList.append(emptyItem("还没有角色，先新建一个"));
    return;
  }
  for (const character of state.characters) {
    const item = rosterItem(character.name, `${character.sprites.length} 张立绘`);
    item.classList.toggle("active", character.id === state.editingCharacterId);
    item.addEventListener("click", () => {
      state.editingCharacterId = character.id;
      renderCharacterList();
      renderCharacterEditor();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "item-action danger";
    const armed = Boolean(state.armed[`char-${character.id}`]);
    remove.textContent = armed ? "确认删除？" : "删除";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      armOrRun(
        `char-${character.id}`,
        renderCharacterList,
        () => deleteCharacter(character),
      );
    });
    item.append(remove);
    characterList.append(item);
  }
}

function currentEditingCharacter() {
  return state.characters.find((item) => item.id === state.editingCharacterId);
}

function tagChip(text, onRemove) {
  const chip = document.createElement("span");
  chip.className = "tag-chip";
  chip.textContent = text;
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "×";
  close.title = "移除这个标签";
  close.addEventListener("click", onRemove);
  chip.append(close);
  return chip;
}

function renderSpriteList() {
  const character = currentEditingCharacter();
  spriteList.replaceChildren();
  if (!character) return;
  if (character.sprites.length === 0) {
    spriteList.append(emptyItem("还没有立绘，先上传一张"));
    return;
  }
  for (const sprite of character.sprites) {
    const item = document.createElement("li");
    item.className = "sprite-item";

    const thumb = document.createElement("img");
    thumb.className = "sprite-thumb";
    thumb.src = assetUrl(sprite.file);
    thumb.alt = sprite.file;
    thumb.title = "点一下在舞台上预览";
    // Sprites are a few MB each; only fetch the ones actually scrolled into view.
    thumb.loading = "lazy";
    thumb.decoding = "async";
    thumb.addEventListener("click", () => {
      state.tag = (sprite.tags || [])[0] || "";
      applySprite(sprite.file);
      setStatus(`预览 ${sprite.file}`, false, true);
    });
    item.append(thumb);

    const body = document.createElement("div");
    body.className = "sprite-body";

    const head = document.createElement("div");
    head.className = "sprite-head";
    const nameNode = document.createElement("span");
    nameNode.className = "sprite-name";
    nameNode.textContent = sprite.file;
    // Full name on hover: the label itself is ellipsised for long file names.
    nameNode.title = sprite.file;
    head.append(nameNode);
    if (sprite.is_default) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "默认兜底";
      head.append(badge);
    }
    body.append(head);

    const chips = document.createElement("div");
    chips.className = "tag-chips";
    if ((sprite.tags || []).length === 0) {
      const none = document.createElement("span");
      none.className = "tag-empty";
      none.textContent = "未打标签（AI 不会选中这张）";
      chips.append(none);
    }
    for (const tag of sprite.tags || []) {
      chips.append(
        tagChip(tag, () =>
          void updateSpriteTags(
            sprite,
            sprite.tags.filter((existing) => existing !== tag),
          ),
        ),
      );
    }
    body.append(chips);

    const addRow = document.createElement("div");
    addRow.className = "tag-add";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "输入标签后回车";
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const value = input.value.trim();
      input.value = "";
      if (!value || (sprite.tags || []).includes(value)) return;
      void updateSpriteTags(sprite, [...(sprite.tags || []), value]);
    });
    addRow.append(input);
    body.append(addRow);

    const presets = document.createElement("div");
    presets.className = "tag-presets";
    const available = PRESET_TAGS.filter(
      (tag) => !(sprite.tags || []).includes(tag),
    ).slice(0, 8);
    for (const tag of available) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `+${tag}`;
      button.addEventListener("click", () =>
        void updateSpriteTags(sprite, [...(sprite.tags || []), tag]),
      );
      presets.append(button);
    }
    body.append(presets);

    const actions = document.createElement("div");
    actions.className = "sprite-actions";
    if (!sprite.is_default) {
      const makeDefault = document.createElement("button");
      makeDefault.type = "button";
      makeDefault.className = "mini";
      makeDefault.textContent = "设为默认";
      makeDefault.addEventListener(
        "click",
        () => void updateSpriteDefault(sprite),
      );
      actions.append(makeDefault);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "mini danger";
    const armed = Boolean(state.armed[`sprite-${sprite.id}`]);
    remove.textContent = armed ? "确认删除？" : "删除";
    remove.addEventListener("click", () =>
      armOrRun(
        `sprite-${sprite.id}`,
        renderSpriteList,
        () => deleteSprite(sprite),
      ),
    );
    actions.append(remove);
    body.append(actions);

    item.append(body);
    spriteList.append(item);
  }
}

function renderCharacterEditor() {
  const character = currentEditingCharacter();
  if (!character) {
    characterEditor.hidden = true;
    return;
  }
  characterEditor.hidden = false;
  characterTitle.textContent = `角色：${character.name}`;
  renderSpriteList();
}

function renderBackgroundList() {
  backgroundList.replaceChildren();
  if (state.backgrounds.length === 0) {
    backgroundList.append(emptyItem("还没有背景，先上传一张"));
    return;
  }
  for (const background of state.backgrounds) {
    const item = document.createElement("li");
    item.className = "background-item";
    const thumb = document.createElement("img");
    thumb.className = "background-thumb";
    thumb.src = assetUrl(background.file);
    thumb.alt = background.name;
    thumb.loading = "lazy";
    thumb.decoding = "async";
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = background.name;
    name.title = background.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "item-action danger";
    const armed = Boolean(state.armed[`bg-${background.id}`]);
    remove.textContent = armed ? "确认删除？" : "删除";
    remove.addEventListener("click", () =>
      armOrRun(
        `bg-${background.id}`,
        renderBackgroundList,
        () => deleteBackground(background),
      ),
    );
    item.append(thumb, name, remove);
    backgroundList.append(item);
  }
}

async function loadLibrary() {
  try {
    const data = await bridge.apiGet("library");
    state.characters = data.characters || [];
    state.backgrounds = data.backgrounds || [];
    if (state.editingCharacterId && !currentEditingCharacter()) {
      state.editingCharacterId = 0;
    }
    if (!state.editingCharacterId && state.characters.length) {
      state.editingCharacterId = state.characters[0].id;
    }
    renderCharacterList();
    renderCharacterEditor();
    renderBackgroundList();
    renderBindingEditor();
    renderGuide();
  } catch (error) {
    rosterHint.textContent = `加载素材库失败：${error.message}`;
  }
}

async function createCharacter() {
  const name = newCharacterName.value.trim();
  if (!name) {
    setStatus("先输入角色名字", true);
    return;
  }
  try {
    const result = await bridge.apiPost("character/create", { name });
    markLocalEdit();
    newCharacterName.value = "";
    state.editingCharacterId = result.id;
    await loadLibrary();
    setTab("characters");
    setStatus(`已新建角色 ${name}，上传立绘后打上情绪标签`, false, true);
  } catch (error) {
    setStatus(`新建角色失败：${error.message}`, true);
  }
}

/**
 * Register the example art that ships inside the plugin. Useful on a fresh
 * install and after the user removed the examples from their library.
 */
async function loadDefaultAssets() {
  try {
    const result = await bridge.apiPost("defaults", {});
    const added = result.added || { sprites: 0, backgrounds: 0 };
    markLocalEdit();
    await loadLibrary();
    // Select the example character so its sprites are visible right away. It is
    // recognised by its bundled file names instead of a duplicated name string.
    const character = state.characters.find((item) =>
      (item.sprites || []).some((sprite) =>
        String(sprite.file).startsWith("default_"),
      ),
    );
    if (character) {
      state.editingCharacterId = character.id;
      renderCharacterEditor();
    }
    setTab("characters");
    setStatus(
      added.sprites || added.backgrounds
        ? `已载入示例素材：${added.sprites} 张立绘、${added.backgrounds} 张背景，可以直接用，也可以换成自己的图`
        : "示例素材已经在库里了",
      false,
      true,
    );
  } catch (error) {
    setStatus(`载入示例素材失败：${error.message}`, true);
  }
}

async function deleteCharacter(character) {
  try {
    await bridge.apiPost("character/delete", { id: character.id });
    markLocalEdit();
    if (state.editingCharacterId === character.id) {
      state.editingCharacterId = 0;
    }
    await loadLibrary();
    await loadFriends();
    setStatus(`已删除角色 ${character.name}`, false, true);
  } catch (error) {
    setStatus(`删除失败：${error.message}`, true);
  }
}

async function uploadSprite(file) {
  const character = currentEditingCharacter();
  if (!character || !file) return;
  setStatus(`正在上传 ${file.name}…`);
  try {
    const uploaded = await bridge.upload("upload", file);
    markLocalEdit();
    await bridge.apiPost("sprite/add", {
      character_id: character.id,
      file: uploaded.filename,
      tags: [],
    });
    await loadLibrary();
    setStatus(`已上传 ${uploaded.filename}，现在给它打情绪标签`, false, true);
  } catch (error) {
    setStatus(`上传失败：${error.message}`, true);
  }
}

async function updateSpriteTags(sprite, tags) {
  try {
    await bridge.apiPost("sprite/update", { id: sprite.id, tags });
    markLocalEdit();
    await loadLibrary();
  } catch (error) {
    setStatus(`保存标签失败：${error.message}`, true);
  }
}

async function updateSpriteDefault(sprite) {
  try {
    await bridge.apiPost("sprite/update", { id: sprite.id, is_default: true });
    markLocalEdit();
    await loadLibrary();
    renderStage();
  } catch (error) {
    setStatus(`设置默认失败：${error.message}`, true);
  }
}

async function deleteSprite(sprite) {
  try {
    await bridge.apiPost("sprite/delete", { id: sprite.id });
    markLocalEdit();
    await loadLibrary();
    renderStage();
  } catch (error) {
    setStatus(`删除失败：${error.message}`, true);
  }
}

async function uploadBackground(file) {
  if (!file) return;
  setStatus(`正在上传 ${file.name}…`);
  try {
    const uploaded = await bridge.upload("upload", file);
    markLocalEdit();
    await bridge.apiPost("background/add", {
      file: uploaded.filename,
      name: uploaded.filename.replace(/\.[^.]+$/, ""),
    });
    await loadLibrary();
    setStatus(`已加入背景库：${uploaded.filename}`, false, true);
  } catch (error) {
    setStatus(`上传失败：${error.message}`, true);
  }
}

async function deleteBackground(background) {
  try {
    await bridge.apiPost("background/delete", { id: background.id });
    markLocalEdit();
    await loadLibrary();
    await loadFriends();
  } catch (error) {
    setStatus(`删除失败：${error.message}`, true);
  }
}

/* ------------------------------ sending ------------------------------ */

async function sendText(text, optionId, button) {
  if (!state.umo || !text) return;
  if (button) button.disabled = true;
  try {
    await bridge.apiPost("send", {
      umo: state.umo,
      text,
      option_id: optionId,
    });
    setStatus("");
    clearChoices();
  } catch (error) {
    if (button) button.disabled = false;
    setStatus(`发送失败：${error.message}`, true);
  }
}

async function regenerate() {
  if (!state.umo) return;
  setThinking("重新思考中");
  try {
    await bridge.apiPost("regenerate", { umo: state.umo });
  } catch (error) {
    setStatus(`重新生成失败：${error.message}`, true);
  }
}

/* ------------------------------ events ------------------------------ */

function handleEvent(payload) {
  if (!payload || typeof payload !== "object") return;
  const type = payload.type;

  if (type === "bindings_changed") {
    void loadFriends();
    return;
  }
  if (type === "library_changed") {
    // Ignore the echo of our own edit so the tag input keeps focus.
    if (Date.now() - state.localEditAt < 1500) return;
    void loadLibrary();
    void loadFriends();
    return;
  }
  if (type === "connected") return;
  if (type === "settings_changed") {
    if (Date.now() - state.localEditAt < 1500) return;
    void loadSettings();
    return;
  }
  if (payload.umo && payload.umo !== state.umo) return;

  if (type === "friend_message") {
    clearChoices();
    pushMessage("friend", payload.text, true);
    setThinking();
    return;
  }
  if (type === "thinking") {
    setThinking();
    return;
  }
  if (type === "options") {
    if (state.binding && payload.affection !== undefined) {
      state.binding.affection = payload.affection;
    }
    if (payload.tag !== undefined) {
      state.tag = payload.tag || "";
    }
    renderStage();
    renderChoices(payload.options || []);
    setStatus("");
    return;
  }
  if (type === "my_message") {
    pushMessage("me", payload.text, false);
    return;
  }
  if (type === "affection") {
    showAffectionEffect(payload.delta, payload.value, payload.angle);
    return;
  }
  if (type === "error") {
    setStatus(payload.message || "出错了", true);
  }
}

function subscribe() {
  bridge
    .subscribeSSE(
      "events",
      {
        onOpen() {
          if (statusBox.textContent.startsWith("实时连接断开")) setStatus("");
        },
        onMessage(event) {
          handleEvent(event.parsed);
        },
        onError() {
          setStatus("实时连接断开，3 秒后重连…", true);
          window.setTimeout(subscribe, 3000);
        },
      },
      {},
    )
    .catch((error) => {
      setStatus(`实时连接失败：${error.message}`, true);
    });
}

/* ------------------------------ wiring ------------------------------ */

openDrawerButton.addEventListener("click", () => setDrawerOpen(true));
closeDrawerButton.addEventListener("click", () => setDrawerOpen(false));
drawerScrim.addEventListener("click", () => setDrawerOpen(false));

tabButtons.friends.addEventListener("click", () => setTab("friends"));
tabButtons.persona.addEventListener("click", () => setTab("persona"));
tabButtons.characters.addEventListener("click", () => setTab("characters"));
tabButtons.backgrounds.addEventListener("click", () => setTab("backgrounds"));

saveSelfPersonaButton.addEventListener("click", () => void saveSelfPersona());
saveRelationButton.addEventListener("click", () => void saveRelation());

dialogue.addEventListener("click", () => advance());
vn.addEventListener("click", (event) => {
  if (
    event.target.closest(
      ".vn-choices, .vn-composer, .vn-top, .vn-dialogue, .log-window, .affect-layer",
    )
  ) {
    return;
  }
  advance();
});

openLogButton.addEventListener("click", () => void openLog());
closeLogButton.addEventListener("click", () => {
  logWindow.hidden = true;
  setStatus("");
});
logWindow.addEventListener("click", (event) => {
  // Clicking the backdrop area of the log window closes it too.
  if (event.target === logWindow) logWindow.hidden = true;
});

composerToggle.addEventListener("click", () => {
  setComposerOpen(composer.hidden);
});

manualSend.addEventListener("click", () => {
  const text = manualInput.value.trim();
  if (!text) return;
  manualInput.value = "";
  void sendText(text, undefined, null);
});

manualInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    manualSend.click();
  }
});

bindCharacter.addEventListener("change", () => void applyBinding());
bindBackground.addEventListener("change", () => void applyBinding());

newCharacterButton.addEventListener("click", () => void createCharacter());
loadDefaultsButton.addEventListener("click", () => void loadDefaultAssets());
newCharacterName.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    void createCharacter();
  }
});

spriteUpload.addEventListener("change", () => {
  const file = spriteUpload.files?.[0];
  spriteUpload.value = "";
  void uploadSprite(file);
});

backgroundUpload.addEventListener("change", () => {
  const file = backgroundUpload.files?.[0];
  backgroundUpload.value = "";
  void uploadBackground(file);
});

unbindButton.addEventListener("click", async () => {
  const binding = state.binding;
  if (!binding) return;
  if (!state.unbindArmed) {
    state.unbindArmed = true;
    unbindButton.classList.add("armed");
    unbindButton.textContent = "再点一次确认解绑";
    return;
  }
  try {
    await bridge.apiPost("unbind", { umo: binding.umo });
    state.binding = null;
    state.umo = "";
    state.character = null;
    state.background = null;
    state.tag = "";
    state.messages = [];
    state.cursor = -1;
    state.unbindArmed = false;
    unbindButton.classList.remove("armed");
    unbindButton.textContent = "解绑这位好友";
    clearChoices();
    renderStage();
    await loadFriends();
    setStatus("已解绑", false, true);
  } catch (error) {
    setStatus(`解绑失败：${error.message}`, true);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLTextAreaElement) return;
  if (event.target instanceof HTMLInputElement) return;
  if (event.key === "Escape") {
    if (!logWindow.hidden) {
      logWindow.hidden = true;
      return;
    }
    if (!composer.hidden) {
      setComposerOpen(false);
      return;
    }
    setDrawerOpen(false);
    return;
  }
  if (!logWindow.hidden) return;
  if (event.key === " " || event.key === "Enter") {
    event.preventDefault();
    advance();
    return;
  }
  if (/^[1-9]$/.test(event.key)) {
    const button = choicesBox.querySelectorAll(".choice")[Number(event.key) - 1];
    if (button && !button.disabled) button.click();
  }
});

async function main() {
  const context = await bridge.ready();
  document.documentElement.setAttribute(
    "data-theme",
    context?.isDark ? "dark" : "light",
  );
  await loadFriends();
  await loadLibrary();
  await loadSettings();
  setTab("friends");
  subscribe();
  const first = bindingList.querySelector("li[data-umo]");
  if (first?.dataset.umo) {
    await openScene(first.dataset.umo);
  }
}

void main();
