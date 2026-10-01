<script lang="ts">
  import { onMount } from "svelte";
  import ActionRow from "./lib/ActionRow.svelte";
  import DevicesEditor from "./lib/DevicesEditor.svelte";
  import {
    api,
    deviceList,
    type Action,
    type DeviceInfo,
    type DriverInfo,
    type Effect,
    type RawConfig,
    type Status,
  } from "./lib/api";

  // Every event the config accepts; the daemon reports which ones it handles.
  const EVENTS = ["UserPromptSubmit", "Notification", "Stop"] as const;
  const DESCRIPTIONS: Record<string, string> = {
    UserPromptSubmit: "You sent a prompt and Claude starts working.",
    Notification: "Claude needs permission or input.",
    Stop: "Claude finished responding.",
  };
  const TEST_EFFECT: Effect = { type: "flash", color: "#ffffff", duration: 1, times: 2 };

  let status = $state<Status | null>(null);
  let drivers = $state<DriverInfo[]>([]);
  let statuses = $state<DeviceInfo[]>([]);
  let saved = $state<RawConfig | null>(null);
  let draft = $state<RawConfig>({});
  let configPath = $state("");
  let loadError = $state<string | null>(null);
  let offline = $state(false);
  let saving = $state(false);
  let message = $state<{ text: string; kind: "ok" | "error" | "info" } | null>(null);
  let logs = $state("");
  let logsOpen = $state(false);

  const dirty = $derived(saved !== null && JSON.stringify(draft) !== JSON.stringify(saved));
  const deviceNames = $derived(Object.keys(draft.devices ?? {}));
  const hooksMissing = $derived(
    status?.hooks.installed ? status.hooks.handled.filter((e) => !status!.hooks.installed!.includes(e)) : [],
  );

  function notify(text: string, kind: "ok" | "error" | "info" = "info") {
    message = { text, kind };
  }

  async function loadConfig() {
    const response = await api.config();
    configPath = response.path;
    loadError = response.error;
    saved = response.config ?? null;
    draft = structuredClone(response.config ?? { version: 1, devices: {}, events: {} });
    // An editable "saved" baseline for a new or invalid file, so dirtiness still works.
    saved ??= structuredClone($state.snapshot(draft));
  }

  async function refreshDevices() {
    try {
      statuses = await api.devices();
      offline = false;
    } catch (e) {
      // 409 means the file on disk is invalid, which loadConfig already reports.
      offline = e instanceof TypeError;
    }
  }

  onMount(() => {
    (async () => {
      try {
        [status, drivers] = await Promise.all([api.status(), api.drivers()]);
        await loadConfig();
        await refreshDevices();
      } catch (e) {
        offline = true;
        notify(`Can't reach the daemon: ${(e as Error).message}`, "error");
      }
    })();
    const timer = setInterval(refreshDevices, 2000);
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    addEventListener("beforeunload", warn);
    return () => {
      clearInterval(timer);
      removeEventListener("beforeunload", warn);
    };
  });

  function cleaned(config: RawConfig): RawConfig {
    const copy = structuredClone($state.snapshot(config)) as RawConfig;
    for (const [event, actions] of Object.entries(copy.events ?? {})) {
      if (actions.length === 0) delete copy.events![event];
    }
    return copy;
  }

  async function save() {
    saving = true;
    try {
      const result = await api.saveConfig(cleaned(draft));
      await loadConfig();
      notify(result.backup ? `Saved. Previous version: ${result.backup}` : "Saved.", "ok");
    } catch (e) {
      notify(`Not saved: ${(e as Error).message}`, "error");
    } finally {
      saving = false;
    }
  }

  function revert() {
    if (saved) draft = structuredClone($state.snapshot(saved));
    message = null;
  }

  async function preview(devices: string[], effect: Effect) {
    try {
      // Unsaved device settings are sent along, so previews match what's on screen.
      const unsaved = $state.snapshot(draft.devices ?? {});
      const result = await api.preview(devices, $state.snapshot(effect), unsaved);
      notify(`Playing on ${result.queued.join(", ")}.`);
    } catch (e) {
      notify(`Preview failed: ${(e as Error).message}`, "error");
    }
  }

  async function simulate(event: string) {
    try {
      const result = await api.simulate(event);
      notify(
        result.queued.length
          ? `Simulating ${event} (saved config) on ${result.queued.join(", ")}.`
          : `No saved effects for ${event}.`,
      );
    } catch (e) {
      notify(`Simulate failed: ${(e as Error).message}`, "error");
    }
  }

  function addAction(event: string) {
    draft.events ??= {};
    draft.events[event] ??= [];
    const device = deviceNames[0] ?? [];
    draft.events[event].push({ device, effect: { type: "flash", color: "#00ff60", duration: 1, times: 2 } });
  }

  function removeAction(event: string, index: number) {
    draft.events?.[event]?.splice(index, 1);
  }

  async function loadLogs() {
    try {
      logs = (await api.logs(300)).text;
    } catch (e) {
      logs = `Can't load logs: ${(e as Error).message}`;
    }
  }

  function actionsFor(event: string): Action[] {
    return draft.events?.[event] ?? [];
  }
</script>

<main>
  <header class="row">
    <h1>ambient</h1>
    <span class="spacer"></span>
    {#if offline}
      <span class="chip"><span class="dot danger"></span>daemon offline</span>
    {:else if status}
      <span class="chip" title="pid {status.pid}"><span class="dot ok"></span>daemon v{status.version}</span>
      {#if status.hooks.installed === null}
        <span class="chip" title={status.hooks.settings_path}><span class="dot danger"></span>settings.json unreadable</span>
      {:else if hooksMissing.length === 0}
        <span class="chip" title={status.hooks.settings_path}><span class="dot ok"></span>hooks installed</span>
      {:else}
        <span class="chip" title={status.hooks.settings_path}
          ><span class="dot warn"></span>hooks missing: {hooksMissing.join(", ")}</span
        >
      {/if}
    {/if}
  </header>
  {#if status && hooksMissing.length > 0}
    <p class="muted">Run <code>ambient install-hooks</code> so Claude Code sends its events here.</p>
  {/if}
  <p class="muted">Config: <code>{configPath}</code></p>

  {#if loadError}
    <div class="card error">
      The config file is invalid, so the editor starts empty. Saving replaces it (a <code>.bak</code> copy is
      kept).<br />
      <code>{loadError}</code>
    </div>
  {/if}

  <section class="card">
    <div class="card-head"><h2>Events</h2></div>
    {#each EVENTS as event (event)}
      {@const handled = status?.hooks.handled.includes(event) ?? true}
      <div class="event">
        <div class="row">
          <h3>{event}</h3>
          {#if !handled}<span class="chip warn" title="ambient fire ignores this event for now">not handled yet</span>{/if}
          <span class="spacer"></span>
          <button type="button" onclick={() => simulate(event)} title="Play the saved effects for this event"
            >Simulate</button
          >
          <button type="button" onclick={() => addAction(event)}>Add effect</button>
        </div>
        <p class="muted">{DESCRIPTIONS[event]}</p>
        {#each actionsFor(event) as action, i (i)}
          <ActionRow
            bind:action={draft.events![event]![i]!}
            devices={deviceNames}
            onpreview={() => preview(deviceList(action), action.effect)}
            onremove={() => removeAction(event, i)}
          />
        {/each}
      </div>
    {/each}
  </section>

  <DevicesEditor
    bind:config={draft}
    {drivers}
    {statuses}
    {dirty}
    onpreview={(name) => preview([name], TEST_EFFECT)}
    onpaired={loadConfig}
    {notify}
  />

  <section class="card">
    <div class="card-head">
      <h2>Logs</h2>
      <div class="row">
        {#if logsOpen}<button type="button" onclick={loadLogs}>Refresh</button>{/if}
        <button
          type="button"
          onclick={() => {
            logsOpen = !logsOpen;
            if (logsOpen) loadLogs();
          }}>{logsOpen ? "Hide" : "Show"}</button
        >
      </div>
    </div>
    {#if logsOpen}<pre class="logs">{logs || "(empty)"}</pre>{/if}
  </section>
</main>

<div class="bar" class:visible={dirty || message}>
  <div class="bar-inner row">
    {#if message}
      <span class={message.kind === "info" ? "muted" : message.kind}>{message.text}</span>
    {/if}
    <span class="spacer"></span>
    {#if dirty}
      <span class="muted">Unsaved changes</span>
      <button type="button" onclick={revert} disabled={saving}>Revert</button>
      <button type="button" class="primary" onclick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
    {:else if message}
      <button type="button" class="icon" onclick={() => (message = null)} aria-label="dismiss">✕</button>
    {/if}
  </div>
</div>

<style>
  header {
    gap: 8px;
  }
  .spacer {
    flex: 1;
  }
  .event {
    border-top: 1px solid var(--border);
    padding: 12px 0;
  }
  .event:first-of-type {
    border-top: none;
    padding-top: 0;
  }
  .event p {
    margin: 4px 0 0;
  }
  .bar {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--surface);
    border-top: 1px solid var(--border);
    transform: translateY(100%);
    transition: transform 0.15s ease;
  }
  .bar.visible {
    transform: none;
  }
  .bar-inner {
    max-width: 980px;
    margin: 0 auto;
    padding: 10px 16px;
  }
</style>
