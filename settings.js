// The score settings panel, in groups that fold away. Each setting has a box for an exact number and
// a slider. The sliders go in steps of 1, 1.5, 2, 3, 5 and 7 times a power of ten, so one slider covers
// small and huge numbers. A group's heading says how many of its settings differ from the defaults.
// Settings that add no points when the page loads (0, or no post limit) are hidden to keep the panel
// short; each group's "Add a setting" brings them back. The similarity measures (marked measure: true)
// only shape the two similarity penalties, so they're shown whenever either penalty is.
// Each mode has its own settings. The panel shows and changes the active mode's: the one you picked
// in its Mode list, or with Auto, the mode the calendar turns on (see server.active_mode). With no
// mode on, the feed is empty and there are no settings to show.
const GROUPS = [
  { id: "feed", title: "Feed", open: true },
  { id: "opened", title: "Opened and next up", open: true },
  { id: "tags", title: "Tags", open: true },
  { id: "kind", title: "Kind, length and age" },
  { id: "modes", title: "Mode changes" },
  { id: "tasks", title: "Tasks" },
  { id: "similarity", title: "Similarity" },
];
const SETTINGS = [
  { group: "feed", key: "post_limit", label: "Post limit", before: "show at most", after: "posts", blankIsMax: true,
    hint: "0 empties the feed; 1 shows one post at a time. Leave empty for no limit." },

  { group: "opened", key: "seen_bonus", label: "Opened", after: "points",
    hint: "For posts you've opened. Tagging doesn't count as opening." },
  { group: "opened", key: "next_up_bonus", label: "Next up", after: "points",
    hint: "For the post after the one you last opened in a subscription." },
  { group: "opened", key: "new_sub_bonus", label: "New subscription", after: "points",
    hint: "For posts from a subscription you've never opened anything from." },
  { group: "opened", key: "series_penalty", label: "Unstarted series", after: "points",
    hint: "For a later part of a series when you haven't opened an earlier one." },

  { group: "tags", key: "green_points", label: "Show again soon (green)", after: "points" },
  { group: "tags", key: "white_points", label: "Maybe later (white)", after: "points" },
  { group: "tags", key: "red_points", label: "Don't show again (red)", after: "points" },

  { group: "kind", key: "video_bonus", label: "Video", after: "points",
    hint: "YouTube, Jellyfin, NRK TV and other video pages." },
  { group: "kind", key: "youtube_shorts_bonus", label: "YouTube Shorts", after: "points",
    hint: "Added on top of Video." },
  { group: "kind", key: "audio_bonus", label: "Audio", after: "points",
    hint: "Podcast episodes and other audio files." },
  { group: "kind", key: "image_bonus", label: "Image", after: "points",
    hint: "Links straight to a picture." },
  { group: "kind", key: "article_bonus", label: "Article", after: "points",
    hint: "Everything else." },
  { group: "kind", key: "length_per_minute", label: "Length", after: "points per minute from",
    extra: { key: "length_target_minutes", after: "minutes" },
    hint: "Below 0 favors posts near the target length. Unknown lengths get none." },
  { group: "kind", key: "middle_author_points", label: "Middle author", after: "points on papers with at least",
    extra: { key: "middle_author_min_count", after: "authors" },
    hint: "For a paper from an author you follow who is neither its first nor its last author. −1000 hides such papers like the red tag." },
  { group: "kind", key: "age_per_year", label: "Age", after: "points per year",
    hint: "Below 0 favors newer posts." },

  { group: "modes", key: "event_lead_minutes", label: "Lead time", after: "minutes",
    hint: "This mode turns on this long before an event it's on for, so you're ready when it starts. Free time doesn't use it." },
  { group: "modes", key: "past_mode_change_per_minute", label: "Past the mode change", after: "points per minute",
    hint: "For each minute a post would run past when the calendar next changes the mode. The post you're viewing is exempt." },

  { group: "tasks", key: "task_deadline_bonus", label: "Deadline", after: "points when due, ramping up over",
    extra: { key: "task_deadline_days", after: "days" },
    hint: "From 0 at the start of the ramp to the full points at the deadline. Overdue tasks keep the full points until they're completed or cancelled in the calendar." },
  { group: "tasks", key: "task_priority_bonus", label: "Priority", after: "points at priority 1",
    hint: "A ninth less per level, down to priority 9." },
  { group: "tasks", key: "task_bonus", label: "Every task", after: "points" },

  { group: "similarity", key: "similar_penalty", label: "Like something opened", after: "points, fading over",
    extra: { key: "similar_hours", after: "hours" },
    hint: "Per 100% similarity to each post you opened recently." },
  { group: "similarity", key: "above_similarity_penalty", label: "Like posts above", after: "points",
    hint: "Per 100% similarity to each post above it, to mix up the feed." },
  { group: "similarity", measure: true, key: "similar_base_percent", label: "Same subscription", after: "% alike",
    hint: "Where similarity starts for posts from one subscription; the weights below fill the rest. Other subscriptions never match." },
  { group: "similarity", measure: true, key: "similar_series_percent", label: "Same series", after: "% alike",
    hint: "Used instead for posts in one series, when higher." },
  { group: "similarity", measure: true, key: "similar_days", label: "Publish window", before: "alike within", after: "days" },
  { group: "similarity", measure: true, key: "similar_publish_weight", label: "Publish time weight", after: "" },
  { group: "similarity", measure: true, key: "similar_length_weight", label: "Length weight", after: "",
    hint: "Relative to the publish time weight. Unknown lengths are left out." },
];
// Every setting, including the second box some have, such as Similar's hours
const KEYS = SETTINGS.flatMap(({ key, extra }) => (extra ? [key, extra.key] : [key]));
let limits = {};  // each term's {min, max, integer}, from the server
let saved = {};  // as saved on the server
let draft = {};  // as shown in the panel
let defaults = {};
let modeList = [];  // [{name, when, terms}], the modes, highest priority first
let mode = { name: null, source: "none", situation: null, event: null };  // the active mode, whose settings the panel shows
// The settings in effect in a mode: its own. With no mode on, the defaults, which aren't shown.
const termsOf = (name, data) => ({ ...data.defaults, ...data.modes.find((m) => m.name === name)?.terms });

// Slider positions: 0, then 1, 1.5, 2, 3, 5, 7 times each power of ten from 10^lo up to 10^hi,
// mirrored below 0 for terms that may be negative.
function steps(lo, hi, signed) {
  const up = [];
  for (let e = lo; e < hi; e++) for (const n of [1, 1.5, 2, 3, 5, 7]) up.push(Number((n * 10 ** e).toPrecision(2)));
  up.push(10 ** hi);
  return signed ? [...up.map((v) => -v).reverse(), 0, ...up] : [0, ...up];
}
const SIGNED_STEPS = steps(0, 3, true);  // ±1 to ±1000 points
const BONUS_STEPS = steps(0, 3, false);  // 1 to 1000 points
// Whole numbers of posts, ending in the maximum: no limit
const POST_LIMIT_STEPS = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000, 10_000_000];

// The slider position closest to a typed number, on the slider's log scale.
function nearestStep(list, value) {
  const f = (x) => (x === 0 ? 0 : Math.sign(x) * (Math.log10(Math.abs(x)) + 4));
  let best = 0;
  list.forEach((v, i) => { if (Math.abs(f(v) - f(value)) < Math.abs(f(list[best]) - f(value))) best = i; });
  return best;
}

const fmt = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 3 }).replaceAll(",", " ").replace("-", "−");
const sameTerms = (a, b) => KEYS.every((key) => a[key] === b[key]);
const inRange = (key, v) => Number.isFinite(v) && v >= limits[key].min && v <= limits[key].max && (!limits[key].integer || Number.isInteger(v));
const controls = {};
const groupEls = {};

// The group headings' fold state stays as you left it, in this browser.
function groupOpen(group) {
  try {
    const stored = JSON.parse(localStorage.getItem("settingsGroups") || "{}");
    if (group.id in stored) return stored[group.id];
  } catch {}
  return !!group.open;
}
function rememberGroup(id, open) {
  try {
    const stored = JSON.parse(localStorage.getItem("settingsGroups") || "{}");
    localStorage.setItem("settingsGroups", JSON.stringify({ ...stored, [id]: open }));
  } catch {}
}

// A number box's value: a blank post limit means no limit (the maximum).
function boxValue(box, key, blankIsMax) {
  if (box.value.trim() === "") return blankIsMax ? limits[key].max : NaN;
  return Number(box.value);
}
function showBox(box, key, value, blankIsMax) {
  if (document.activeElement === box) return;
  box.value = blankIsMax && value === limits[key].max ? "" : value;
}

function settingRow({ key, label, before, after, extra, hint, blankIsMax }) {
  const { min, max } = limits[key];
  const list = key === "post_limit" ? POST_LIMIT_STEPS
    : max <= 100 ? Array.from({ length: max + 1 }, (_, i) => i) : min < 0 ? SIGNED_STEPS : BONUS_STEPS;
  const num = el("input", { type: "number", id: `w-${key}`, step: "any", inputmode: "decimal", min, max,
    ...(blankIsMax ? { placeholder: "none" } : { required: "" }) });
  const slider = el("input", { type: "range", min: 0, max: list.length - 1, step: 1, "aria-label": `${label} slider` });
  const def = el("span", { class: "default" });
  controls[key] = { num, slider, list, def, blankIsMax };
  slider.addEventListener("input", () => {
    showBox(num, key, list[slider.value], blankIsMax);
    change(key, list[slider.value]);
  });
  num.addEventListener("input", () => {
    const v = boxValue(num, key, blankIsMax);
    if (!inRange(key, v)) return;
    slider.value = nearestStep(list, v);
    change(key, v);
  });
  slider.addEventListener("change", save);  // when you let go of the slider
  num.addEventListener("change", save);  // when you press Enter or leave the box
  // A second box, such as how many hours count as recent, without a slider
  let second = [];
  if (extra) {
    const box = el("input", { type: "number", id: `w-${extra.key}`, step: "any", inputmode: "decimal", required: "",
      min: limits[extra.key].min, max: limits[extra.key].max, "aria-label": `${label} ${extra.after}` });
    controls[extra.key] = { num: box };
    box.addEventListener("input", () => {
      const v = boxValue(box, extra.key, false);
      if (inRange(extra.key, v)) change(extra.key, v);
    });
    box.addEventListener("change", save);
    second = [box, extra.after];
  }
  return el("div", { class: "setting", "data-key": key },
    el("label", { for: `w-${key}` }, label),
    el("span", { class: "num" }, before || "", num, after || "", ...second),
    slider,
    el("p", { class: "hint" }, hint ? `${hint} ` : "", def));
}

function buildSettings() {
  document.getElementById("settings").replaceChildren(...GROUPS.map((group) => {
    const count = el("span", { class: "changed" });
    const details = el("details", { class: "group" },
      el("summary", {}, group.title, count),
      ...(group.hint ? [el("p", { class: "hint group-hint" }, group.hint)] : []),
      ...SETTINGS.filter((s) => s.group === group.id).map(settingRow));
    details.open = groupOpen(group);
    details.addEventListener("toggle", () => rememberGroup(group.id, details.open));
    groupEls[group.id] = { details, count };
    return details;
  }));
}

// One "Add a setting" list below the groups, with the hidden settings under their groups' names.
// Picking one shows it, opens its group and puts the cursor in its box.
const addSetting = document.getElementById("add-setting");
addSetting.addEventListener("change", () => {
  const key = addSetting.value;  // read before showTerms rebuilds the list
  if (!key) return;
  shownTerms.add(key);
  showTerms();
  const { group } = SETTINGS.find((s) => s.key === key);
  groupEls[group].details.open = true;
  const { num } = controls[key];
  num.focus();
  num.scrollIntoView({ block: "center" });
});

function showSettings(w) {
  for (const { key, extra } of SETTINGS) {
    const { num, slider, list, blankIsMax } = controls[key];
    showBox(num, key, w[key], blankIsMax);
    slider.value = nearestStep(list, w[key]);
    if (extra) showBox(controls[extra.key].num, extra.key, w[extra.key], false);
  }
  showChanged();
}

// Which settings are shown: those that added points when the page loaded, and those you added.
const shownTerms = new Set();
const contributes = (key, w) => (key === "post_limit" ? w[key] < limits[key].max : w[key] !== 0);
function hideUnusedTerms(w) {
  for (const { key, measure } of SETTINGS) if (!measure && contributes(key, w)) shownTerms.add(key);
  showTerms();
}

function showTerms() {
  const similarity = shownTerms.has("similar_penalty") || shownTerms.has("above_similarity_penalty");
  for (const { key, measure } of SETTINGS) {
    document.querySelector(`.setting[data-key="${key}"]`).hidden = measure ? !similarity : !shownTerms.has(key);
  }
  // A group with nothing shown is left out until you add one of its settings. With no mode on, none are.
  const usable = () => !!mode.name;
  for (const group of GROUPS) {
    groupEls[group.id].details.hidden = !usable(group) || !SETTINGS.some((s) => s.group === group.id
      && !document.querySelector(`.setting[data-key="${s.key}"]`).hidden);
  }
  const groups = GROUPS.filter(usable).map((group) => [group,
    SETTINGS.filter((s) => s.group === group.id && !s.measure && !shownTerms.has(s.key))]).filter(([, hidden]) => hidden.length);
  addSetting.replaceChildren(el("option", { value: "" }, "Add a setting…"),
    ...groups.map(([group, hidden]) => el("optgroup", { label: group.title },
      ...hidden.map(({ key, label }) => el("option", { value: key }, label)))));
  addSetting.hidden = !groups.length;
}

// Each group's heading counts its settings that differ from the defaults, so you can see where
// you've changed something without opening every group. The dots on the cards show the tag points.
function showChanged() {
  for (const group of GROUPS) {
    const n = SETTINGS.filter((s) => s.group === group.id
      && [s.key, s.extra?.key].some((k) => k && draft[k] !== defaults[k])).length;
    groupEls[group.id].count.textContent = n ? `${n} changed` : "";
  }
  if (typeof relabelDots === "function") relabelDots();
}

function panelSay(text, isError = false) {
  const status = document.getElementById("panel-status");
  status.textContent = text;
  status.className = isError ? "error" : "";
}

// A setting moved: show the feed in its new order right away, from the top.
function change(key, value) {
  draft = { ...draft, [key]: value };
  showChanged();
  panelSay("");
  load({ reorder: true });
}

// Saves run one after another, so the last change wins even when two start together, such as a
// number box saving as you click Reset.
let saving = Promise.resolve();
function save() {
  saving = saving.then(saveNow);
  return saving;
}

async function saveNow() {
  const invalid = KEYS.find((key) => !inRange(key, draft[key]));
  if (invalid) {
    const { label } = SETTINGS.find((s) => s.key === invalid || s.extra?.key === invalid);
    const { min, max, integer } = limits[invalid];
    return panelSay(`${label} must be a ${integer ? "whole " : ""}number from ${fmt(min)} to ${fmt(max)}.`, true);
  }
  if (sameTerms(draft, saved)) return;
  const { ok, data } = await api("/api/settings", { terms: draft, mode: mode.name });
  if (!ok) return panelSay(`Couldn't save: ${data.error || "unknown error"}`, true);
  modeList = data.modes;
  saved = termsOf(mode.name, data);
  panelSay(`Saved for ${mode.name}.`);
}

let rankerShown = null;
function rankerNote(ranker) {
  if (ranker === rankerShown) return;
  rankerShown = ranker;
  if (ranker !== "score") panelSay(`The feed is ranked by ${ranker}, which doesn't use the score terms.`);
}

async function loadSettings() {
  const { ok, data } = await api("/api/settings");
  if (!ok) throw new Error(data.error);
  defaults = data.defaults;
  limits = data.limits;
  buildSettings();
  for (const { key, extra, blankIsMax } of SETTINGS) {
    const value = blankIsMax && defaults[key] === limits[key].max ? "no limit" : fmt(defaults[key]);
    const second = extra ? ` and ${fmt(defaults[extra.key])} ${extra.after}` : "";
    controls[key].def.textContent = `Default ${value}${second}.`;
  }
  showMode(data);
}

// Show the active mode's settings, from /api/settings or /api/modes. Settings that add no points in
// this mode are hidden again, as each mode uses different ones.
function showMode(data) {
  modeList = data.modes;
  mode = data.mode;
  saved = termsOf(mode.name, data);
  draft = { ...saved };
  showSettings(draft);
  shownTerms.clear();
  hideUnusedTerms(saved);
  showModeBar();
  document.getElementById("mode-settings").hidden = !mode.name;
  document.getElementById("reset").hidden = !mode.name;
  document.getElementById("reset").textContent = `Reset ${mode.name} to defaults`;
}

// The Mode list: Auto and the modes, highest priority first, each with a radio button to pick
// it and the modes with buttons to move them up or down. Under Auto, the mode the calendar has on is
// marked "on now".
const modeListEl = document.getElementById("mode-list");
function showModeBar() {
  const picked = mode.source === "chosen" ? mode.name : "";
  const row = (value, label, extra = []) => {
    const radio = el("input", { type: "radio", name: "mode", value });
    radio.checked = value === picked;
    radio.addEventListener("change", () => modeRequest({ choose: value || null }));
    const now = !picked && value === mode.name ? [el("span", { class: "now" }, "on now")] : [];
    return el("div", { class: `mode-row${radio.checked ? " checked" : ""}` },
      el("label", {}, radio, el("span", { class: "name" }, label), ...now), ...extra);
  };
  const move = (name, i, by) => {
    const up = by < 0;
    const button = el("button", { type: "button", class: "move", "data-move": name, "data-dir": up ? "up" : "down",
                                  "aria-label": `Move ${name} ${up ? "up" : "down"}`, title: up ? "Higher priority" : "Lower priority" },
                      up ? "↑" : "↓");
    button.disabled = up ? i === 0 : i === modeList.length - 1;
    button.addEventListener("click", () => moveMode(name, i + by, up ? "up" : "down"));
    return button;
  };
  modeListEl.replaceChildren(
    row("", "Auto: follow the calendar"),
    ...modeList.map((m, i) => row(m.name, m.name, [move(m.name, i, -1), move(m.name, i, 1)])));
  document.getElementById("mode-order-hint").hidden = modeList.length < 2;
  document.getElementById("mode-remove").hidden = !mode.name;
  document.getElementById("mode-remove").textContent = `Remove ${mode.name}`;
  whenBox.hidden = !mode.name;
  const toggles = modeList.find((m) => m.name === mode.name)?.when || {};
  for (const box of whenBoxes) box.checked = !!toggles[box.dataset.situation];
  document.getElementById("mode-hint").textContent =
    mode.source === "calendar" ? `These are ${mode.name}'s settings, on ${situationText(mode)}.`
    : mode.source === "chosen" ? `These are ${mode.name}'s settings, until you pick Auto.`
    : modeList.length ? `No mode is on, so the feed is empty${mode.until ? ` until ${mode.next} turns on at ${when(mode.until)}` : ""}. Pick a mode to use it, or to choose when the calendar turns it on.`
    : "There are no modes, so the feed is empty. Add one with New mode….";
}

// Why the calendar turned a mode on, and until when it changes to which, for the hint.
function situationText({ situation, event, until, next }) {
  const what = situation === "free_time" ? "during free time" : `during “${event || "an event"}”`;
  return until ? `${what} until ${when(until)}, then ${next ?? "no mode"}` : what;
}

// Mode changes save what you were changing first, then show the new mode's settings and feed.
async function modeRequest(body) {
  await saving;
  const { ok, data } = await api("/api/modes", body);
  if (!ok) {
    showModeBar();
    return panelSay(data.error || "Couldn't change the mode.", true);
  }
  panelSay("");
  showMode(data);
  modeChosen(data.mode.name);
  load({ reorder: true });
}

document.getElementById("mode-add").addEventListener("click", () => {
  const from = mode.name ? `${mode.name}'s settings` : "the default settings";
  const name = prompt(`Name of the new mode. It starts with ${from}. With Auto, you choose below when the calendar turns it on.`);
  if (name !== null && name.trim()) modeRequest({ add: name.trim(), copy_from: mode.name });
});

// Moving a mode changes which wins when several could be on, so the feed may change too. The focus
// stays on the button you pressed, or the other one when that's gone (at the top or bottom).
async function moveMode(name, to, dir) {
  await modeRequest({ move: name, to });
  const other = dir === "up" ? "down" : "up";
  const find = (d) => [...modeListEl.querySelectorAll("button.move")].find((b) => b.dataset.move === name && b.dataset.dir === d && !b.disabled);
  (find(dir) || find(other))?.focus();
}

// Each toggle is saved when you tick or untick it. It may turn the mode on or off, so the feed is
// shown again.
const whenBox = document.getElementById("mode-when");
const whenBoxes = [...whenBox.querySelectorAll("input[data-situation]")];
for (const box of whenBoxes) {
  box.addEventListener("change", async () => {
    await saving;
    const { ok, data } = await api("/api/modes", { when: { [box.dataset.situation]: box.checked }, mode: mode.name });
    if (!ok) {
      box.checked = !box.checked;
      return panelSay(data.error || "Couldn't save that.", true);
    }
    panelSay("Saved.");
    showMode(data);
    modeChosen(data.mode.name);
    load({ reorder: true });
  });
}

document.getElementById("mode-remove").addEventListener("click", () => {
  if (confirm(`Remove the ${mode.name} mode and its settings?`)) modeRequest({ remove: mode.name });
});

// The feed says which mode it used. When that's another one, such as when a calendar event turns a
// mode on, the panel switches to its settings.
let modeCheck = null;
function syncMode(fresh) {
  if (!fresh || modeCheck || !Object.keys(limits).length) return;
  if (fresh.name === mode.name && fresh.source === mode.source && fresh.situation === mode.situation
      && fresh.event === mode.event && fresh.until === mode.until && fresh.next === mode.next) return;
  modeCheck = api("/api/settings").then(({ ok, data }) => { if (ok) showMode(data); })
    .finally(() => { modeCheck = null; });
}

document.getElementById("reset").addEventListener("click", async () => {
  const reset = { ...defaults };
  if (sameTerms(draft, reset)) return panelSay(`These are ${mode.name}'s defaults.`);
  draft = reset;
  showSettings(draft);
  await save();
  load({ reorder: true });
});

// The panel stays open or closed across reloads, in this browser.
function setPanel(open) {
  document.getElementById("panel").hidden = !open;
  document.body.classList.toggle("panel-open", open);
  document.getElementById("panel-toggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("scorePanel", open ? "open" : "closed"); } catch {}
}
document.getElementById("panel-toggle").addEventListener("click", () => setPanel(document.getElementById("panel").hidden));
document.getElementById("panel-close").addEventListener("click", () => setPanel(false));
let panelOpen = false;
try { panelOpen = localStorage.getItem("scorePanel") === "open"; } catch {}
setPanel(panelOpen);

