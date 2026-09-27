const state = { mode: null, session: null, card: null, revealed: false, shutdownToken: null };

const elements = {
  homeView: document.querySelector("#home-view"),
  studyView: document.querySelector("#study-view"),
  flashcard: document.querySelector("#flashcard"),
  completePanel: document.querySelector("#complete-panel"),
  questionPanel: document.querySelector("#question-panel"),
  answerPanel: document.querySelector("#answer-panel"),
  serverStatus: document.querySelector("#server-status"),
  learningRemaining: document.querySelector("#learning-remaining"),
  learningDescription: document.querySelector("#learning-description"),
  reviewDue: document.querySelector("#review-due"),
  totalWords: document.querySelector("#total-words"),
  apiStatus: document.querySelector("#api-status"),
  checkApiButton: document.querySelector("#check-api-button"),
  learnButton: document.querySelector("#learn-button"),
  reviewButton: document.querySelector("#review-button"),
  generateButton: document.querySelector("#generate-button"),
  modeLabel: document.querySelector("#mode-label"),
  progressLabel: document.querySelector("#progress-label"),
  progressBar: document.querySelector("#progress-bar"),
  cardWord: document.querySelector("#card-word"),
  examplesList: document.querySelector("#examples-list"),
  answerInput: document.querySelector("#answer-input"),
  answerWord: document.querySelector("#answer-word"),
  phonetic: document.querySelector("#phonetic"),
  morphologyParts: document.querySelector("#morphology-parts"),
  morphologyAnalysis: document.querySelector("#morphology-analysis"),
  meaningsList: document.querySelector("#meanings-list"),
  completeTitle: document.querySelector("#complete-title"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务器返回了无法识别的内容");
  }
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function setHidden(element, hidden) {
  element.classList.toggle("hidden", hidden);
}

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadDashboard() {
  showHome();
  try {
    const dashboard = await api("/api/dashboard");
    state.shutdownToken = dashboard.shutdown_token;
    elements.serverStatus.textContent = "数据已连接";
    elements.learningRemaining.textContent = dashboard.learning.remaining;
    elements.reviewDue.textContent = dashboard.review.due_count;
    elements.totalWords.textContent = dashboard.total_words;
    elements.learningDescription.textContent = dashboard.learning.word_count
      ? `本计划共 ${dashboard.learning.word_count} 个新词，进度会自动保存。`
      : "还没有学习计划，请先生成今日新词。";
    elements.apiStatus.textContent = dashboard.deepseek_configured
      ? "DeepSeek 已配置，可以生成新的学习计划。"
      : "尚未配置 DeepSeek；仍可体验已有卡片。";
    elements.apiStatus.classList.remove("inline-error", "inline-success");
    elements.checkApiButton.disabled = !dashboard.deepseek_configured;
    elements.learnButton.disabled = dashboard.learning.remaining === 0;
    elements.reviewButton.disabled = dashboard.review.due_count === 0 && !dashboard.review.session_active;
  } catch (error) {
    elements.serverStatus.textContent = "连接失败";
    showToast(error.message, true);
  }
}

function showHome() {
  state.mode = null;
  setHidden(elements.homeView, false);
  setHidden(elements.studyView, true);
}

async function startSession(mode) {
  try {
    const session = await api(`/api/sessions/${mode}`);
    state.mode = mode;
    setHidden(elements.homeView, true);
    setHidden(elements.studyView, false);
    renderSession(session);
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderSession(session) {
  state.session = session;
  state.card = session.card;
  state.revealed = false;
  elements.modeLabel.textContent = session.mode === "learning" ? "新词学习" : "到期复习";
  elements.progressLabel.textContent = `${session.completed} / ${session.total}`;
  const completed = !session.card;
  const progress = session.total ? Math.min((session.completed / session.total) * 100, 100) : 100;
  elements.progressBar.style.width = `${progress}%`;
  setHidden(elements.flashcard, completed);
  setHidden(elements.completePanel, !completed);
  if (completed) {
    elements.completeTitle.textContent = session.mode === "learning" ? "本次学习完成" : "本次复习完成";
    return;
  }
  renderQuestion(session.card);
}

function renderQuestion(card) {
  setHidden(elements.questionPanel, false);
  setHidden(elements.answerPanel, true);
  elements.answerInput.value = "";
  elements.cardWord.textContent = card.word;
  elements.examplesList.replaceChildren();
  let count = 1;
  card.meanings.forEach((meaning) => {
    meaning.examples.forEach((example) => {
      const item = create("div", "example-item");
      item.append(create("span", "example-number", String(count).padStart(2, "0")));
      item.append(create("p", "", example));
      elements.examplesList.append(item);
      count += 1;
    });
  });
}

function revealAnswer() {
  if (!state.card) return;
  state.revealed = true;
  setHidden(elements.questionPanel, true);
  setHidden(elements.answerPanel, false);
  renderAnswer(state.card);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderAnswer(card) {
  elements.answerWord.textContent = card.word;
  elements.phonetic.textContent = card.phonetic || "暂无音标";
  elements.morphologyParts.replaceChildren();
  const parts = card.morphology?.parts || [];
  if (parts.length) {
    parts.forEach((part) => {
      const item = create("div", "morphology-part");
      item.append(create("strong", "", part.form || "—"));
      item.append(create("small", "", part.type || "构词成分"));
      item.append(create("span", "", `${part.meaning_en || ""} · ${part.meaning_zh || ""}`));
      elements.morphologyParts.append(item);
    });
  } else {
    elements.morphologyParts.append(create("p", "muted", "该词不适合进行简单的词缀拆分。"));
  }
  elements.morphologyAnalysis.replaceChildren();
  (card.morphology?.analysis || []).forEach((step, index) => {
    const row = create("div", "analysis-step");
    row.append(create("span", "step-index", String(index + 1)));
    row.append(create("p", "", step));
    elements.morphologyAnalysis.append(row);
  });
  elements.meaningsList.replaceChildren();
  card.meanings.forEach((meaning, index) => {
    const item = create("article", "meaning-item");
    const copy = create("div", "meaning-copy");
    const meta = create("div", "meaning-meta");
    meta.append(create("span", "meaning-number", String(index + 1).padStart(2, "0")));
    if (meaning.part_of_speech) meta.append(create("span", "part-speech", meaning.part_of_speech));
    copy.append(meta);
    copy.append(create("p", "meaning-en", meaning.meaning_en));
    copy.append(create("p", "meaning-zh", meaning.meaning_zh));
    meaning.examples.forEach((example) => copy.append(create("p", "meaning-example", example)));
    const judgement = create("fieldset", "judgement");
    judgement.dataset.meaningId = meaning.id;
    judgement.append(create("legend", "", "你的回答"));
    const correctLabel = create("label", "choice-option correct-option");
    const correctInput = document.createElement("input");
    correctInput.type = "radio";
    correctInput.name = `judgement-${meaning.id}`;
    correctInput.value = "true";
    correctLabel.append(correctInput, create("span", "", "答对了"));
    const wrongLabel = create("label", "choice-option wrong-option");
    const wrongInput = document.createElement("input");
    wrongInput.type = "radio";
    wrongInput.name = `judgement-${meaning.id}`;
    wrongInput.value = "false";
    wrongLabel.append(wrongInput, create("span", "", "没答对"));
    judgement.append(correctLabel, wrongLabel);
    item.append(copy, judgement);
    elements.meaningsList.append(item);
  });
}

async function submitResult() {
  if (!state.card) return;
  const results = {};
  let missing = false;
  elements.meaningsList.querySelectorAll("fieldset[data-meaning-id]").forEach((fieldset) => {
    const selected = fieldset.querySelector("input:checked");
    if (!selected) {
      missing = true;
      fieldset.classList.add("needs-choice");
      return;
    }
    fieldset.classList.remove("needs-choice");
    results[fieldset.dataset.meaningId] = selected.value === "true";
  });
  if (missing) {
    showToast("请为每个释义选择“答对了”或“没答对”", true);
    return;
  }
  await submitCard(`/api/sessions/${state.mode}/answer`, {
    word: state.card.word,
    meaning_results: results,
  });
}

async function markKnowWell() {
  if (!state.card || !window.confirm(`确认将 ${state.card.word} 设为“熟练掌握”吗？`)) return;
  await submitCard(`/api/words/${encodeURIComponent(state.card.word)}/know-well`, { mode: state.mode });
}

async function submitCard(path, payload) {
  try {
    const response = await api(path, { method: "POST", body: JSON.stringify(payload) });
    const labels = { again: "再次复习", hard: "困难", good: "良好", easy: "简单", know_well: "熟练掌握" };
    showToast(`已记录：${labels[response.result.feedback]}`);
    renderSession(response.session);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function generatePlan() {
  if (!window.confirm("将生成10个新的 CET-6 单词并替换当前学习计划，是否继续？")) return;
  elements.generateButton.disabled = true;
  elements.generateButton.textContent = "正在生成，请稍候…";
  try {
    await api("/api/learning-plan/generate", { method: "POST", body: "{}" });
    showToast("新的10词学习计划已生成");
    await loadDashboard();
  } catch (error) {
    elements.apiStatus.textContent = `生成失败：${error.message}`;
    elements.apiStatus.classList.add("inline-error");
    showToast(error.message, true);
  } finally {
    elements.generateButton.disabled = false;
    elements.generateButton.textContent = "生成10个 CET-6 新词";
  }
}

async function checkApiConnection() {
  elements.checkApiButton.disabled = true;
  elements.checkApiButton.textContent = "正在检测…";
  try {
    const result = await api("/api/deepseek/check", { method: "POST", body: "{}" });
    elements.apiStatus.textContent = `${result.message}，模型：${result.model}`;
    elements.apiStatus.classList.remove("inline-error");
    elements.apiStatus.classList.add("inline-success");
    showToast("DeepSeek API 连接正常");
  } catch (error) {
    elements.apiStatus.textContent = `连接失败：${error.message}`;
    elements.apiStatus.classList.remove("inline-success");
    elements.apiStatus.classList.add("inline-error");
    showToast(error.message, true);
  } finally {
    elements.checkApiButton.disabled = false;
    elements.checkApiButton.textContent = "测试 API 连接";
  }
}

async function shutdownServer() {
  if (!window.confirm("确认关闭 WordRoot 服务器吗？学习进度会保留。")) return;
  const button = document.querySelector("#shutdown-button");
  button.disabled = true;
  button.textContent = "正在关闭…";
  try {
    await api("/api/server/shutdown", {
      method: "POST",
      headers: { "X-Shutdown-Token": state.shutdownToken || "" },
      body: "{}",
    });
    document.querySelector(".shell").classList.add("hidden");
    setHidden(document.querySelector("#shutdown-view"), false);
    setTimeout(() => window.close(), 500);
  } catch (error) {
    button.disabled = false;
    button.textContent = "关闭程序";
    showToast(error.message, true);
  }
}

function closeCurrentTab() {
  window.open("", "_self");
  window.close();
}

let toastTimer;
function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  setHidden(elements.toast, false);
  toastTimer = setTimeout(() => setHidden(elements.toast, true), 3600);
}

document.querySelector("#home-button").addEventListener("click", loadDashboard);
document.querySelector("#back-button").addEventListener("click", loadDashboard);
document.querySelector("#complete-home-button").addEventListener("click", loadDashboard);
elements.learnButton.addEventListener("click", () => startSession("learning"));
elements.reviewButton.addEventListener("click", () => startSession("review"));
elements.generateButton.addEventListener("click", generatePlan);
elements.checkApiButton.addEventListener("click", checkApiConnection);
document.querySelector("#shutdown-button").addEventListener("click", shutdownServer);
document.querySelector("#close-tab-button").addEventListener("click", closeCurrentTab);
document.querySelector("#reveal-button").addEventListener("click", revealAnswer);
document.querySelector("#submit-result-button").addEventListener("click", submitResult);
document.querySelector("#know-well-button").addEventListener("click", markKnowWell);

loadDashboard();
