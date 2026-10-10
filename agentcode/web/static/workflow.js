/* Workflow 页面：选流程 → 看流程图 → 跑起来 → 每个节点实时变色 → 回看历史运行。
   全部走同源接口，不需要额外依赖。 */

"use strict";

const wfState = {
  flows: [],
  current: null,
  nodeElements: new Map(),
  source: null,
  running: false,
};

const el = {
  flows: document.getElementById("wf-flows"),
  title: document.getElementById("wf-title"),
  desc: document.getElementById("wf-desc"),
  agents: document.getElementById("wf-agents"),
  form: document.getElementById("wf-form"),
  input: document.getElementById("wf-input"),
  run: document.getElementById("wf-run"),
  status: document.getElementById("wf-status"),
  nodes: document.getElementById("wf-nodes"),
  runs: document.getElementById("wf-run-list"),
};

const KIND_LABEL = {
  agent: "智能体",
  command: "命令",
  judge: "判定",
  end: "终点",
};

const STATUS_LABEL = {
  pending: "待运行",
  running: "跑着",
  ok: "完成",
  failed: "失败",
  skipped: "跳过",
};

async function wfApi(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `请求失败（${response.status}）`);
  }
  return data;
}

function text(node, value) {
  node.textContent = value;
}

/* ------------------------------------------------------------ 流程清单 */

async function loadFlows() {
  const data = await wfApi("/api/workflows");
  wfState.flows = data.workflows || [];
  el.flows.replaceChildren(
    ...wfState.flows.map((flow) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "wf-flow";
      button.dataset.name = flow.name;
      button.innerHTML = `
        <strong></strong>
        <span></span>`;
      button.querySelector("strong").textContent = flow.title;
      button.querySelector("span").textContent = `${flow.agents.length} 个 Agent · ${flow.nodes} 个节点`;
      button.addEventListener("click", () => selectFlow(flow.name));
      return button;
    })
  );
  if (wfState.flows.length) {
    await selectFlow(wfState.flows[0].name);
  }
}

async function selectFlow(name) {
  const data = await wfApi(`/api/workflow?name=${encodeURIComponent(name)}`);
  wfState.current = data.workflow;
  for (const button of el.flows.querySelectorAll(".wf-flow")) {
    button.classList.toggle("is-active", button.dataset.name === name);
  }
  renderFlow();
  const sample = wfState.current.name === "code_review" ? "写一个判断素数的函数" : "写一个计算器";
  el.input.value = `request=${sample}`;
  if (!wfState.running) {
    // 流程正在跑的时候别把状态提示擦掉——那是用户唯一能看出"在跑"的地方
    text(el.status, "");
  }
}

/* ------------------------------------------------------------ 流程图 */

function renderFlow() {
  const flow = wfState.current;
  text(el.title, flow.title);
  text(el.desc, flow.description || "");
  text(el.agents, `用到的智能体：${flow.agents.join("、") || "（无）"}`);
  wfState.nodeElements = new Map();

  const items = flow.nodes.map((node) => {
    const li = document.createElement("li");
    li.className = "wf-node";
    li.dataset.node = node.name;

    const head = document.createElement("div");
    head.className = "wf-node-head";
    const name = document.createElement("strong");
    name.textContent = node.title;
    head.append(name);

    const kind = document.createElement("span");
    kind.className = "wf-kind";
    kind.textContent = `${KIND_LABEL[node.kind] || node.kind}${node.agent ? ` · ${node.agent}` : ""}`;
    head.append(kind);

    const badge = document.createElement("span");
    badge.className = "wf-badge";
    badge.textContent = STATUS_LABEL.pending;
    head.append(badge);

    const detail = document.createElement("p");
    detail.className = "wf-node-detail";
    detail.textContent = node.output ? `产出写入状态：${node.output}` : "流程终点";

    const edges = document.createElement("ul");
    edges.className = "wf-edges";
    for (const edge of node.edges) {
      const item = document.createElement("li");
      const loop = edge.max_times ? `（最多 ${edge.max_times} 次）` : "";
      item.textContent = `→ ${edge.label}${loop}`;
      edges.append(item);
    }

    li.append(head, detail, edges);
    wfState.nodeElements.set(node.name, li);
    return li;
  });
  el.nodes.replaceChildren(...items);
}

function nodeName(name) {
  return String(name || "");
}

function setNodeStatus(name, status, message) {
  const item = wfState.nodeElements.get(nodeName(name));
  if (!item) {
    return;
  }
  item.dataset.status = status;
  item.classList.toggle("is-running", status === "running");
  const badge = item.querySelector(".wf-badge");
  if (badge) {
    badge.textContent = STATUS_LABEL[status] || status;
  }
  const detail = item.querySelector(".wf-node-detail");
  if (detail && message) {
    detail.textContent = message;
  }
}

function resetNodes() {
  for (const [name, item] of wfState.nodeElements) {
    item.dataset.status = "pending";
    item.classList.remove("is-running");
    const badge = item.querySelector(".wf-badge");
    if (badge) {
      badge.textContent = STATUS_LABEL.pending;
    }
    const node = wfState.current.nodes.find((entry) => entry.name === name);
    const detail = item.querySelector(".wf-node-detail");
    if (detail && node) {
      detail.textContent = node.output ? `产出写入状态：${node.output}` : "流程终点";
    }
  }
}

/* ------------------------------------------------------------ 运行 */

function parseInputs(raw) {
  const inputs = {};
  for (const line of String(raw || "").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const index = trimmed.indexOf("=");
    if (index <= 0) {
      continue;
    }
    inputs[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return inputs;
}

function closeStream() {
  if (wfState.source) {
    wfState.source.close();
    wfState.source = null;
  }
}

async function startRun(event) {
  event.preventDefault();
  if (!wfState.current || wfState.running) {
    return;
  }
  const llm = el.form.querySelector('input[name="wf-llm"]:checked').value;
  wfState.running = true;
  el.run.disabled = true;
  resetNodes();
  text(el.status, "正在提交…");
  closeStream();

  let started;
  try {
    started = await wfApi("/api/workflow/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: wfState.current.name,
        inputs: parseInputs(el.input.value),
        llm,
      }),
    });
  } catch (error) {
    wfState.running = false;
    el.run.disabled = false;
    text(el.status, `启动失败：${error.message}`);
    return;
  }

  text(el.status, `已开始：${started.run_id}（${llm === "mock" ? "离线演示" : "真实模型"}）`);
  const source = new EventSource(started.stream);
  wfState.source = source;

  source.addEventListener("node", (message) => {
    const data = JSON.parse(message.data);
    if (data.status === "running") {
      setNodeStatus(data.name, "running", `${data.title} 正在跑…`);
      return;
    }
    if (data.status === "ok") {
      setNodeStatus(data.name, "ok", `${data.title} 完成（${Math.round(data.duration_ms || 0)} ms）`);
      return;
    }
    if (data.status === "failed") {
      setNodeStatus(data.name, "failed", `${data.title} 失败：${data.error || ""}`);
      return;
    }
    if (data.status === "skipped") {
      setNodeStatus(data.name, "skipped", `${data.title} 跳过：${data.skipped_reason || ""}`);
    }
  });

  source.addEventListener("warning", (message) => {
    const data = JSON.parse(message.data);
    text(el.status, data.message || "有警告");
  });

  source.addEventListener("saved", async (message) => {
    const data = JSON.parse(message.data);
    closeStream();
    wfState.running = false;
    el.run.disabled = false;
    const mark = { succeeded: "成功", partial: "部分完成", failed: "失败" }[data.status] || data.status;
    text(
      el.status,
      `这次运行：${mark}，${data.steps} 步。${data.error ? `原因：${data.error}` : ""}`
    );
    await refreshRuns();
    if (data.run_id) {
      await showRecord(data.run_id);
    }
  });

  source.addEventListener("done", (message) => {
    // 注意：saved 事件紧跟在 done 后面（引擎先报"跑完"，落盘后再报"saved"）。
    // 这里千万不能关流，否则最后那条"记录已保存"会被丢掉。
    const data = JSON.parse(message.data);
    if (data.status === "failed") {
      text(el.status, `这次运行失败了：${data.error || "原因不明"}`);
    } else {
      text(el.status, "流程跑完了，正在保存运行记录…");
    }
  });

  source.onerror = () => {
    const wasRunning = wfState.running;
    closeStream();
    wfState.running = false;
    el.run.disabled = false;
    if (wasRunning) {
      text(el.status, "连接中断了：流程还在后台跑，刷新页面或看「最近的运行」即可。");
    }
  };
}

/* ------------------------------------------------------------ 运行记录 */

async function refreshRuns() {
  const data = await wfApi("/api/workflow/runs?limit=10");
  const runs = data.runs || [];
  el.runs.replaceChildren(
    ...runs.map((run) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "wf-run";
      button.dataset.run = run.run_id;
      const mark = { succeeded: "成功", partial: "部分完成", failed: "失败" }[run.status] || run.status;
      button.innerHTML = `<strong></strong><span></span>`;
      button.querySelector("strong").textContent = `${run.workflow} · ${mark}`;
      button.querySelector("span").textContent = `${run.run_id} · ${run.steps} 步`;
      button.addEventListener("click", () => showRecord(run.run_id, button));
      return button;
    })
  );
  if (!runs.length) {
    const empty = document.createElement("p");
    empty.className = "wf-sub";
    empty.textContent = "还没有跑过。选一条流程，点「运行这条流程」。";
    el.runs.append(empty);
  }
}

async function showRecord(runId, button) {
  let record;
  try {
    record = await wfApi(`/api/workflow/record?run_id=${encodeURIComponent(runId)}`);
  } catch (error) {
    text(el.status, `读取运行记录失败：${error.message}`);
    return;
  }
  if (button) {
    for (const item of el.runs.querySelectorAll(".wf-run")) {
      item.classList.toggle("is-active", item === button);
    }
  }
  for (const node of record.nodes || []) {
    const status = node.status === "ok" ? "ok" : node.status;
    const note =
      node.status === "skipped"
        ? `${node.title} 跳过：${node.skipped_reason || ""}`
        : node.error
          ? `${node.title} 失败：${node.error}`
          : `${node.title} · ${node.answer ? String(node.answer).slice(0, 40) : "（没有文本产出）"}`;
    setNodeStatus(node.name, status, note);
  }
  const mark = { succeeded: "成功", partial: "部分完成", failed: "失败" }[record.status] || record.status;
  text(el.status, `运行 ${record.run_id}：${mark}（${record.steps} 步）${record.error ? `　原因：${record.error}` : ""}`);
}

/* ------------------------------------------------------------ 启动 */

el.form.addEventListener("submit", startRun);
loadFlows()
  .then(refreshRuns)
  .catch((error) => {
    if (String(error.message).includes("登录")) {
      // 页面本身不需要登录就能打开，但接口要：给个明确的去处，别让人对着空列表发呆
      el.status.innerHTML = '请先登录：<a href="/">去登录页</a>';
      return;
    }
    text(el.status, `加载失败：${error.message}`);
  });
