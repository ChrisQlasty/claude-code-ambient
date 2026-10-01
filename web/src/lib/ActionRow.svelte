<script lang="ts">
  import { deviceList, fullHex, type Action, type EffectType } from "./api";

  let {
    action = $bindable(),
    devices,
    onpreview,
    onremove,
  }: {
    action: Action;
    devices: string[];
    onpreview: () => void;
    onremove: () => void;
  } = $props();

  const selected = $derived(deviceList(action));

  function toggle(name: string) {
    const next = selected.includes(name)
      ? selected.filter((d) => d !== name)
      : devices.filter((d) => d === name || selected.includes(d));
    // A single device is written as a plain name, the common hand-written form.
    action.device = next.length === 1 ? next[0]! : next;
  }

  function setType(type: EffectType) {
    action.effect.type = type;
    if (type === "solid") delete action.effect.times;
  }

  function setNumber(field: "duration" | "times" | "brightness", value: number) {
    // A cleared input is NaN; keep the last valid value instead of saving null.
    if (Number.isFinite(value)) action.effect[field] = value;
  }

  function setBrightness(enabled: boolean) {
    if (enabled) action.effect.brightness = 100;
    else delete action.effect.brightness;
  }
</script>

<div class="action">
  <div class="row devices">
    {#each devices as name (name)}
      <button
        type="button"
        class="chip"
        class:on={selected.includes(name)}
        aria-pressed={selected.includes(name)}
        onclick={() => toggle(name)}>{name}</button
      >
    {/each}
    {#each selected.filter((d) => !devices.includes(d)) as missing (missing)}
      <span class="chip error" title="not a configured device">{missing}</span>
    {/each}
  </div>
  <div class="row">
    <label class="field"
      >Effect
      <select value={action.effect.type} onchange={(e) => setType(e.currentTarget.value as EffectType)}>
        <option value="solid">solid</option>
        <option value="flash">flash</option>
        <option value="pulse">pulse</option>
      </select>
    </label>
    <label class="field"
      >Color
      <input
        type="color"
        value={fullHex(action.effect.color)}
        oninput={(e) => (action.effect.color = e.currentTarget.value)}
      />
    </label>
    <label class="field"
      >Duration (s)
      <input
        type="number"
        min="0.1"
        max="60"
        step="0.1"
        value={action.effect.duration ?? 1}
        oninput={(e) => setNumber("duration", e.currentTarget.valueAsNumber)}
      />
    </label>
    {#if action.effect.type !== "solid"}
      <label class="field"
        >Times
        <input
          type="number"
          min="1"
          max="50"
          step="1"
          value={action.effect.times ?? 1}
          oninput={(e) => setNumber("times", e.currentTarget.valueAsNumber)}
        />
      </label>
    {/if}
    <label class="field"
      >Brightness
      <span class="row">
        <input
          type="checkbox"
          checked={action.effect.brightness !== undefined}
          onchange={(e) => setBrightness(e.currentTarget.checked)}
          aria-label="custom brightness"
        />
        {#if action.effect.brightness !== undefined}
          <input
            type="range"
            min="0"
            max="100"
            value={action.effect.brightness}
            oninput={(e) => setNumber("brightness", e.currentTarget.valueAsNumber)}
          />
          <span class="muted">{action.effect.brightness}%</span>
        {:else}
          <span class="muted">default</span>
        {/if}
      </span>
    </label>
    <span class="spacer"></span>
    <button type="button" onclick={onpreview} disabled={selected.length === 0}>Preview</button>
    <button type="button" class="icon danger" onclick={onremove} aria-label="remove effect">✕</button>
  </div>
</div>

<style>
  .action {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 8px;
  }
  .spacer {
    flex: 1;
  }
  .devices:empty::before {
    content: "No devices configured yet.";
    color: var(--muted);
    font-size: 0.85rem;
  }
</style>
