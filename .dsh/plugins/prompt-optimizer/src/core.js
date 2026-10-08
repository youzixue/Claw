// The state machine is independent of React and never sends a chat message.
export const MAX_DRAFT_CHARS = 16000;
export const MAX_HISTORY = 30;

export function editable(input) {
  return input?.phase === "plain" && (input.occurrences?.length ?? 0) === 0;
}

export function disabledReason(input, model, busy) {
  if (busy) return "正在优化";
  if (!input?.draft?.trim()) return "请输入提示词后再优化";
  if (!editable(input)) return "请使用纯文本提示词；命令或引用芯片不会被改写";
  if (input.draft.length > MAX_DRAFT_CHARS) return "提示词超过 16000 字符，请先缩短";
  if (!model?.provider || !model?.model) return "请先选择模型";
  return "";
}

export function createOptimizerController({ readInput, replace, request, readModel }) {
  let history = [];
  let active = null;
  let snapshot = Object.freeze({ busy: false, count: 0, message: "", error: false, confirmUndo: false });
  const listeners = new Set();
  function publish(patch) {
    snapshot = Object.freeze({ ...snapshot, count: history.length, ...patch });
    for (const fn of listeners) fn();
  }
  function cancel(announce = true) {
    if (!active) return;
    const pending = active;
    active = null;
    pending.abort();
    publish({ busy: false, confirmUndo: false, message: announce ? "已取消，原文未改动" : "", error: false });
  }
  return {
    getSnapshot: () => snapshot,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    observe(input) {
      // Empty means sent/cleared: never resurrect a draft into the next message.
      if (input?.draft === "" && (history.length || active)) {
        cancel(false);
        history = [];
        publish({ count: 0, message: "", error: false, confirmUndo: false });
      }
    },
    cancel,
    dismiss() { publish({ message: "", error: false, confirmUndo: false }); },
    async optimize() {
      const input = readInput();
      const model = readModel();
      if (disabledReason(input, model, active !== null)) return false;
      const before = input.draft;
      const draftRev = input.draftRev;
      const pending = new AbortController();
      active = pending;
      publish({ busy: true, phase: "preparing", outputChars: 0, message: "正在优化提示词…", error: false, confirmUndo: false });
      try {
        const result = await request({ text: before, provider: model.provider, model: model.model }, pending.signal, value => {
          if (active !== pending || pending.signal.aborted) return;
          publish({ phase: value.phase, outputChars: value.outputChars });
        });
        if (active !== pending || pending.signal.aborted) return false;
        const current = readInput();
        if (!editable(current) || current.draft !== before || current.draftRev !== draftRev) {
          publish({ message: "输入已变动，未覆盖。请基于当前文本重新优化。", error: false });
          return false;
        }
        const after = typeof result?.text === "string" ? result.text.trim() : "";
        if (!after) throw new Error("模型未返回有效提示词，原文已保留");
        // A result must remain eligible as the input to the next optimization.
        // Never truncate constraints or install a draft that disables the button.
        if (after.length > MAX_DRAFT_CHARS) throw new Error("优化结果超过 16000 字符，原文已保留，请缩短后重试");
        if (after === before) {
          // Successful completion is separate from editing/undo. Models may
          // legitimately find nothing else to improve; every click still calls them.
          publish({ message: "优化完成，模型未作进一步修改", error: false });
          return true;
        }
        // insertText enforces the live revision atomically and pushes native editor undo.
        if (!replace(after, { start: 0, end: before.length, draftRev })) {
          publish({ message: "输入已变动，未覆盖。请重试。", error: false });
          return false;
        }
        history.push(Object.freeze({ before, after }));
        if (history.length > MAX_HISTORY) history.shift();
        publish({ message: "已优化，可继续优化或撤回上一次", error: false });
        return true;
      } catch (error) {
        if (active === pending && !pending.signal.aborted) {
          publish({ message: error?.message || "优化失败，原文已保留", error: true });
        }
        return false;
      } finally {
        if (active === pending) {
          active = null;
          publish({ busy: false });
        }
      }
    },
    undo(force = false) {
      if (active || history.length === 0) return false;
      const current = readInput();
      if (!editable(current)) return false;
      const previous = history.at(-1);
      if (!force && current.draft !== previous.after) {
        publish({ confirmUndo: true, message: "你已编辑优化结果。撤回会覆盖当前编辑，是否确认？", error: false });
        return false;
      }
      if (!replace(previous.before, { start: 0, end: current.draft.length, draftRev: current.draftRev })) {
        publish({ confirmUndo: false, message: "输入已变动，未撤回，请重试", error: false });
        return false;
      }
      history.pop();
      publish({ confirmUndo: false, message: history.length ? "已撤回一次，可继续撤回" : "已恢复优化前的原文", error: false });
      return true;
    },
    dispose() { cancel(false); listeners.clear(); },
  };
}
