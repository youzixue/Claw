// The native selector records intent for the next request, not the next task.
// Keep that intent available to the composer/optimizer without rerouting live work.
function copyConfig(config) {
  return config ? structuredClone(config) : null;
}

function seedState(session) {
  const header = session.requestHeader();
  const config = copyConfig(header?.config);
  // Match AgentLoop.requestProposal: adapter defaults are not user overrides.
  if (header?.adapterDefaults?.reasoningEffort) delete config.reasoningEffort;
  if (header?.adapterDefaults?.maxTokens) delete config.maxTokens;
  let turn = null;
  let usedTurn = null;
  for (const event of session.snapshotEvents()) {
    if (event.type === "turn/start") turn = event.data.turn;
    if (event.type === "request/header") usedTurn = turn;
  }
  return { config, turn: usedTurn };
}

export function installTaskModelLock(ctx) {
  const live = new Map();
  function attach(agent) {
    // Child routes belong to their launcher; do not override independent subagents.
    if (live.has(agent) || agent.session.header.origin === "subagent") return;
    const state = seedState(agent.session);
    const disposers = [];
    live.set(agent, disposers);
    try {
      disposers.push(agent.ctx.on("agent/inbox/claimed", ({ agent: subject, message, turn }) => {
        if (subject !== agent) return;
        // A real, newly started user task may consume the pending choice. Steering,
        // goal rounds, schedules and question replies cannot change an existing task.
        if (message.source.kind === "user" && turn !== state.turn) {
          state.config = null;
          state.turn = turn;
        }
      }));
      disposers.push(agent.ctx.on("system-prompt/assemble", async (_assembly, context, next) => {
        if (context.agent !== agent) return next();
        const config = state.config;
        const result = await next();
        context.signal?.throwIfAborted();
        if (!config) return result;
        return { ...result, variables: { ...result.variables, provider: config.provider, model: config.model } };
      }, { prepend: true }));
      disposers.push(agent.ctx.on("agent/pre-step", async ({ agent: subject, signal }, next) => {
        if (subject !== agent) return next();
        const config = state.config;
        const result = await next();
        signal.throwIfAborted();
        if (!config || result.kind !== "enter") return result;
        // These are newly assembled notices only, never historical messages.
        // Native intent must not tell the model it switched when routing is pinned.
        return { ...result, messages: result.messages.filter(message => message.source.kind !== "model-selection") };
      }, { prepend: true }));
      disposers.push(agent.ctx.on("agent/request", async ({ agent: subject, turn, signal }, next) => {
        if (subject !== agent) return next();
        const pinned = state.config;
        const proposed = await next();
        signal.throwIfAborted();
        if (!state.config) state.config = copyConfig(pinned ?? proposed);
        state.turn = turn;
        // Clone so downstream preparation cannot mutate the task's routing snapshot.
        return copyConfig(state.config);
      }, { prepend: true }));
    } catch (error) {
      detach(agent);
      throw error;
    }
  }
  function detach(agent) {
    const disposers = live.get(agent);
    if (!disposers) return;
    live.delete(agent);
    for (const dispose of disposers.reverse()) dispose();
  }
  const created = ctx.on("agent/created", ({ agent }) => { attach(agent); });
  const disposed = ctx.on("agent/disposed", ({ agent }) => { detach(agent); });
  try {
    // Loading the fix must protect already-running sessions too, without a restart.
    for (const agent of ctx.agents.list()) attach(agent);
  } catch (error) {
    created();
    disposed();
    for (const agent of live.keys()) detach(agent);
    throw error;
  }
  return () => {
    created();
    disposed();
    for (const agent of live.keys()) detach(agent);
  };
}
