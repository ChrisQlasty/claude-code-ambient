<script lang="ts">
  import {
    api,
    deviceList,
    fullHex,
    type DeviceConfig,
    type DeviceInfo,
    type Discovered,
    type DriverInfo,
    type RawConfig,
  } from "./api";

  let {
    config = $bindable(),
    drivers,
    statuses,
    dirty,
    onpreview,
    onpaired,
    notify,
  }: {
    config: RawConfig;
    drivers: DriverInfo[];
    statuses: DeviceInfo[];
    dirty: boolean;
    onpreview: (name: string) => void;
    onpaired: () => void;
    notify: (message: string, kind?: "ok" | "error") => void;
  } = $props();

  const RESERVED = new Set(["driver", "baseline"]);
  const SECRET = /token|secret|password|key/i;

  let newName = $state("");
  let newDriver = $state("");
  let found = $state<Discovered[] | null>(null);
  let discovering = $state(false);
  let pairing = $state<string | null>(null);
  let newOption = $state<Record<string, { key: string; value: string }>>({});

  const devices = $derived(Object.entries(config.devices ?? {}));
  const driver = (name: string) => drivers.find((d) => d.name === name);
  const statusOf = (name: string) => statuses.find((s) => s.name === name)?.status ?? null;

  function options(device: DeviceConfig): [string, unknown][] {
    return Object.entries(device).filter(([k]) => !RESERVED.has(k));
  }

  // Values are edited as text; keep numbers numeric so the YAML doesn't change type.
  function parseValue(text: string, previous: unknown): unknown {
    if ((typeof previous === "number" || previous === undefined) && /^-?\d+(\.\d+)?$/.test(text)) {
      return Number(text);
    }
    return text;
  }

  function addDevice(name: string, fields: DeviceConfig) {
    name = name.trim();
    if (!name) return notify("Give the device a name.", "error");
    config.devices ??= {};
    if (config.devices[name]) return notify(`A device named "${name}" already exists.`, "error");
    config.devices[name] = fields;
  }

  function removeDevice(name: string) {
    const used = Object.values(config.events ?? {}).some((actions) =>
      actions.some((a) => deviceList(a).includes(name)),
    );
    if (used && !confirm(`"${name}" is used by events. Remove it from them too?`)) return;
    delete config.devices?.[name];
    for (const [event, actions] of Object.entries(config.events ?? {})) {
      const kept = [];
      for (const action of actions) {
        const left = deviceList(action).filter((d) => d !== name);
        if (left.length === 0) continue;
        if (left.length !== deviceList(action).length) {
          action.device = left.length === 1 ? left[0]! : left;
        }
        kept.push(action);
      }
      config.events![event] = kept;
    }
  }

  function setBaseline(device: DeviceConfig, enabled: boolean) {
    if (enabled) device.baseline = { color: "#ffffff", brightness: 100 };
    else delete device.baseline;
  }

  function addOption(name: string, device: DeviceConfig) {
    const draft = newOption[name];
    const key = draft?.key.trim();
    if (!draft || !key) return;
    if (RESERVED.has(key) || key in device) return notify(`"${key}" is already set.`, "error");
    device[key] = parseValue(draft.value, undefined);
    newOption[name] = { key: "", value: "" };
  }

  async function discover() {
    discovering = true;
    try {
      found = await api.discover();
    } catch (e) {
      notify(`Discovery failed: ${(e as Error).message}`, "error");
    } finally {
      discovering = false;
    }
  }

  function addDiscovered(d: Discovered) {
    const base = d.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || d.driver;
    let name = base;
    for (let i = 2; config.devices?.[name]; i++) name = `${base}-${i}`;
    // USB devices report their bus path as "host"; only networked ones (with a port) need it.
    addDevice(name, d.port ? { driver: d.driver, host: d.host } : { driver: d.driver });
  }

  async function pair(name: string, device: DeviceConfig) {
    const info = driver(device.driver);
    pairing = name;
    notify(`Pairing "${name}": ${info?.pairing_hint ?? "follow the device's pairing steps"}…`);
    try {
      const host = typeof device.host === "string" ? device.host : undefined;
      await api.pair(name, { driver: device.driver, host });
      notify(`Paired "${name}" and saved the config.`, "ok");
      onpaired();
    } catch (e) {
      notify(`Pairing failed: ${(e as Error).message}`, "error");
    } finally {
      pairing = null;
    }
  }
</script>

<section class="card">
  <div class="card-head">
    <h2>Devices</h2>
    <button type="button" onclick={discover} disabled={discovering}>
      {discovering ? "Discovering…" : "Discover"}
    </button>
  </div>

  {#if found}
    <div class="found">
      {#if found.length === 0}
        <p class="muted">No devices found.</p>
      {/if}
      {#each found as d (`${d.driver}-${d.host}`)}
        <div class="row">
          <span class="chip">{d.driver}</span>
          <strong>{d.name}</strong>
          <span class="muted">{d.port ? `${d.host}:${d.port}` : d.host}</span>
          <button type="button" onclick={() => addDiscovered(d)}>Add</button>
        </div>
      {/each}
    </div>
  {/if}

  {#each devices as [name, device] (name)}
    {@const status = statusOf(name)}
    {@const info = driver(device.driver)}
    <div class="device">
      <div class="row">
        <span
          class="dot"
          class:ok={status?.last_result === "ok"}
          class:warn={status?.playing}
          class:danger={status?.last_result === "failed"}
          title={status?.playing
            ? "playing"
            : status?.last_result
              ? `last effect: ${status.last_result}`
              : "idle"}
        ></span>
        <h3>{name}</h3>
        <span class="chip">{device.driver}</span>
        {#if !info}<span class="error">unknown driver</span>{/if}
        <span class="spacer"></span>
        <button type="button" onclick={() => onpreview(name)}>Test</button>
        {#if info?.pairable}
          <button
            type="button"
            onclick={() => pair(name, device)}
            disabled={dirty || pairing !== null}
            title={dirty ? "Save or revert your changes first: pairing writes the config" : ""}
          >
            {pairing === name ? "Pairing…" : "Pair"}
          </button>
        {/if}
        <button type="button" class="icon danger" onclick={() => removeDevice(name)} aria-label="remove device"
          >✕</button
        >
      </div>

      <div class="row options">
        {#each options(device) as [key, value] (key)}
          <label class="field"
            >{key}
            <span class="row">
              <input
                type={SECRET.test(key) ? "password" : "text"}
                value={String(value ?? "")}
                oninput={(e) => (device[key] = parseValue(e.currentTarget.value, value))}
              />
              <button type="button" class="icon" onclick={() => delete device[key]} aria-label="remove {key}"
                >✕</button
              >
            </span>
          </label>
        {/each}
        <label class="field"
          >New option
          <span class="row">
            <input
              placeholder="key"
              size="8"
              value={newOption[name]?.key ?? ""}
              oninput={(e) => (newOption[name] = { key: e.currentTarget.value, value: newOption[name]?.value ?? "" })}
            />
            <input
              placeholder="value"
              size="12"
              value={newOption[name]?.value ?? ""}
              oninput={(e) => (newOption[name] = { key: newOption[name]?.key ?? "", value: e.currentTarget.value })}
            />
            <button type="button" onclick={() => addOption(name, device)}>Add</button>
          </span>
        </label>
      </div>

      <div class="row">
        <label class="row muted">
          <input
            type="checkbox"
            checked={!!device.baseline}
            onchange={(e) => setBaseline(device, e.currentTarget.checked)}
          />
          Baseline (restored when the device can't report its state)
        </label>
        {#if device.baseline}
          <input
            type="color"
            value={fullHex(device.baseline.color)}
            oninput={(e) => (device.baseline!.color = e.currentTarget.value)}
          />
          <input
            type="range"
            min="0"
            max="100"
            value={device.baseline.brightness ?? 100}
            oninput={(e) => (device.baseline!.brightness = e.currentTarget.valueAsNumber)}
          />
          <span class="muted">{device.baseline.brightness ?? 100}%</span>
        {/if}
      </div>
    </div>
  {:else}
    <p class="muted">No devices yet. Discover them or add one below.</p>
  {/each}

  <form
    class="row add"
    onsubmit={(e) => {
      e.preventDefault();
      if (!newDriver) return notify("Pick a driver.", "error");
      addDevice(newName, { driver: newDriver });
      newName = "";
    }}
  >
    <input placeholder="device name, e.g. lines" bind:value={newName} />
    <select bind:value={newDriver}>
      <option value="" disabled>driver…</option>
      {#each drivers as d (d.name)}
        <option value={d.name}>{d.name}</option>
      {/each}
    </select>
    <button type="submit">Add device</button>
  </form>
</section>

<style>
  .device {
    border-top: 1px solid var(--border);
    padding: 12px 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .options {
    align-items: flex-end;
  }
  .spacer {
    flex: 1;
  }
  .found {
    background: var(--surface-2);
    border-radius: 8px;
    padding: 8px 12px;
    margin-bottom: 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .add {
    border-top: 1px solid var(--border);
    padding-top: 12px;
  }
</style>
