// Types mirror the daemon's REST API (src/ambient/daemon/server.py) and config models.

export type EffectType = "solid" | "flash" | "pulse";

export interface Effect {
  type: EffectType;
  color: string;
  brightness?: number;
  duration?: number;
  times?: number;
}

export interface Action {
  // The config accepts a single name or a list; the editor keeps whichever form was used.
  device: string | string[];
  effect: Effect;
}

export interface Baseline {
  color: string;
  brightness?: number;
}

export interface DeviceConfig {
  driver: string;
  baseline?: Baseline | null;
  // Driver-specific options (host, token, port, ...).
  [option: string]: unknown;
}

export interface RawConfig {
  version?: number;
  devices?: Record<string, DeviceConfig>;
  events?: Record<string, Action[]>;
}

export interface Status {
  version: string;
  pid: number;
  config_path: string;
  hooks: { settings_path: string; handled: string[]; installed: string[] | null };
}

export interface DriverInfo {
  name: string;
  capabilities: string[];
  pairable: boolean;
  pairing_hint: string;
}

export interface DeviceStatus {
  queued: number;
  playing: boolean;
  last_result: "ok" | "failed" | null;
  last_at: number | null;
}

export interface DeviceInfo {
  name: string;
  driver: string;
  status: DeviceStatus | null;
}

export interface Discovered {
  driver: string;
  name: string;
  host: string;
  port: number;
  [detail: string]: unknown;
}

export interface ConfigResponse {
  path: string;
  exists: boolean;
  config: RawConfig | null;
  error: string | null;
}

export class ApiError extends Error {}

interface ValidationDetail {
  loc: (string | number)[];
  msg: string;
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return (detail as ValidationDetail[])
      .map((d) => `${d.loc.filter((p) => p !== "body").join(".")}: ${d.msg}`)
      .join("; ");
  }
  return JSON.stringify(detail);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // Writes are always JSON: the daemon rejects anything else (see its CSRF note).
  const init: RequestInit = { method, headers: { "content-type": "application/json" } };
  if (method !== "GET") init.body = JSON.stringify(body ?? {});
  const response = await fetch(path, init);
  const text = await response.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail ?? response.statusText;
    throw new ApiError(describe(detail));
  }
  return data as T;
}

export const api = {
  status: () => request<Status>("GET", "api/status"),
  drivers: () => request<DriverInfo[]>("GET", "api/drivers"),
  devices: () => request<DeviceInfo[]>("GET", "api/devices"),
  config: () => request<ConfigResponse>("GET", "api/config"),
  saveConfig: (config: RawConfig) =>
    request<{ backup: string | null }>("PUT", "api/config", { config }),
  discover: (driver?: string) => request<Discovered[]>("POST", "api/discover", { driver }),
  pair: (name: string, body: { driver?: string; host?: string }) =>
    request<{ backup: string | null }>("POST", `api/devices/${encodeURIComponent(name)}/pair`, body),
  preview: (device: string[], effect: Effect, devices: Record<string, DeviceConfig>) =>
    request<{ queued: string[] }>("POST", "api/preview", { device, effect, devices }),
  simulate: (event: string) =>
    request<{ queued: string[] }>("POST", `api/events/${encodeURIComponent(event)}/simulate`),
  logs: (lines = 200) => request<{ path: string; text: string }>("GET", `api/logs?lines=${lines}`),
};

export function deviceList(action: Action): string[] {
  return typeof action.device === "string" ? [action.device] : action.device;
}

/** `#rgb` → `#rrggbb`, which `<input type="color">` requires. */
export function fullHex(color: string): string {
  const hex = color.trim().replace(/^#/, "");
  const six = hex.length === 3 ? [...hex].map((c) => c + c).join("") : hex;
  return /^[0-9a-fA-F]{6}$/.test(six) ? `#${six.toLowerCase()}` : "#000000";
}
