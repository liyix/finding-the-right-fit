/**
 * ALE's benchmark-owned desktop action space exposed as PI extension tools.
 *
 * PI intentionally has no built-in MCP client.  This adapter talks to the
 * same ALE CUA HTTP backend as the upstream cua-desktop MCP bridge and keeps
 * its normalized coordinates, command mapping, tool descriptions, and image
 * result shape.  It adds no task-specific prompt, skill, memory, or retry.
 */

import { Type } from "typebox";

const CUA_URL = (process.env.CUA_SERVER_URL || "http://localhost:5000").replace(/\/+$/, "");
const COORD_MAX = 1000;
let screenSize = null;

const KEY_MAP = {
  ARROWUP: "up", ARROWDOWN: "down", ARROWLEFT: "left", ARROWRIGHT: "right",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  control: "ctrl", Control: "ctrl", CONTROL: "ctrl", Ctrl: "ctrl", CTRL: "ctrl",
  Shift: "shift", SHIFT: "shift", Alt: "alt", ALT: "alt",
  option: "alt", Option: "alt", meta: "cmd", Meta: "cmd",
  command: "cmd", Command: "cmd", win: "cmd", Win: "cmd", super: "cmd", Super: "cmd",
  Enter: "enter", ENTER: "enter", Return: "enter", return: "enter",
  Escape: "esc", escape: "esc", ESC: "esc", Space: "space", SPACE: "space",
  Tab: "tab", TAB: "tab", Backspace: "backspace", BACKSPACE: "backspace",
  Delete: "delete", DELETE: "delete", Home: "home", End: "end",
  PageUp: "page_up", pageup: "page_up", PageDown: "page_down", pagedown: "page_down",
  CapsLock: "caps_lock", capslock: "caps_lock", Insert: "insert", PrintScreen: "print_screen",
};

function normalizeKey(key) {
  return KEY_MAP[key] ?? key.toLowerCase();
}

async function sendCommand(command, params = {}, signal) {
  const timeout = AbortSignal.timeout(30000);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const response = await fetch(`${CUA_URL}/cmd`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, params }),
    signal: combined,
  });
  if (!response.ok) {
    throw new Error(`CUA server HTTP ${response.status}: ${await response.text().catch(() => "")}`);
  }
  let result = null;
  for (const line of (await response.text()).split("\n")) {
    if (!line.startsWith("data: ")) continue;
    try { result = JSON.parse(line.slice(6)); } catch { /* ignore malformed SSE lines */ }
  }
  if (!result) throw new Error(`No valid response for command '${command}'`);
  if (result.success === false) {
    throw new Error(`Command '${command}' failed: ${result.error ?? "unknown error"}`);
  }
  return result;
}

async function getScreenSize(signal) {
  if (!screenSize) {
    const result = await sendCommand("get_screen_size", {}, signal);
    screenSize = result.size ?? { width: result.width, height: result.height };
  }
  return screenSize;
}

async function toAbsolute(coordinate, signal) {
  const screen = await getScreenSize(signal);
  return {
    x: Math.round((coordinate[0] / COORD_MAX) * screen.width),
    y: Math.round((coordinate[1] / COORD_MAX) * screen.height),
  };
}

async function toNormalized(x, y, signal) {
  const screen = await getScreenSize(signal);
  return [
    Math.round((x / screen.width) * COORD_MAX),
    Math.round((y / screen.height) * COORD_MAX),
  ];
}

function textResult(text) {
  return { content: [{ type: "text", text }], details: {} };
}

function register(pi, name, description, parameters, execute) {
  pi.registerTool({
    name,
    label: `ALE CUA: ${name}`,
    description,
    parameters,
    execute: async (toolCallId, params, signal) => execute(params, signal, toolCallId),
  });
}

const Coordinate = Type.Array(Type.Number(), {
  minItems: 2,
  maxItems: 2,
  description: "(x, y) coordinates normalized to [0, 1000].",
});
const Button = Type.Union([Type.Literal("left"), Type.Literal("right"), Type.Literal("middle")]);

export default function (pi) {
  register(pi, "key", "On a desktop, press and release keys. For hotkeys pass multiple keys (e.g. [\"ctrl\", \"c\"]). Valid key names: ctrl, shift, alt, cmd/meta, enter, esc, tab, space, backspace, delete, up, down, left, right, home, end, page_up, page_down, f1-f12, or any single character.",
    Type.Object({ keys: Type.Array(Type.String(), { description: "List of keys to press. Use lowercase names: ctrl, shift, alt, enter, tab, etc." }) }),
    async ({ keys }, signal) => {
      const normalized = keys.map(normalizeKey);
      await sendCommand(normalized.length === 1 ? "press_key" : "hotkey", normalized.length === 1 ? { key: normalized[0] } : { keys: normalized }, signal);
      return textResult(`Pressed: ${normalized.join("+")}`);
    });

  register(pi, "key_down", "On a desktop, press keys down without releasing them. Use with key_up to hold modifiers. Valid key names: ctrl, shift, alt, cmd/meta, enter, esc, tab, space, backspace, delete, up, down, left, right, home, end, page_up, page_down, f1-f12, or any single character.",
    Type.Object({ keys: Type.Array(Type.String(), { description: "List of keys to press down. Use lowercase names: ctrl, shift, alt, etc." }) }),
    async ({ keys }, signal) => {
      const normalized = keys.map(normalizeKey);
      for (const key of normalized) await sendCommand("key_down", { key }, signal);
      return textResult(`Key down: ${normalized.join("+")}`);
    });

  register(pi, "key_up", "On a desktop, release keys that were previously pressed down with key_down. Valid key names: ctrl, shift, alt, cmd/meta, enter, esc, tab, space, backspace, delete, up, down, left, right, home, end, page_up, page_down, f1-f12, or any single character.",
    Type.Object({ keys: Type.Array(Type.String(), { description: "List of keys to release. Use lowercase names: ctrl, shift, alt, etc." }) }),
    async ({ keys }, signal) => {
      const normalized = keys.map(normalizeKey);
      for (const key of normalized) await sendCommand("key_up", { key }, signal);
      return textResult(`Key up: ${normalized.join("+")}`);
    });

  register(pi, "type", "On a desktop, type text content into the currently focused input field.",
    Type.Object({ text: Type.String({ description: "The text content to type." }) }),
    async ({ text }, signal) => {
      await sendCommand("type_text", { text }, signal);
      return textResult(`Typed: \"${text.length > 50 ? `${text.slice(0, 50)}...` : text}\"`);
    });

  register(pi, "hold_key", "On a desktop, hold keys down for a specified duration then release. Valid key names: ctrl, shift, alt, cmd/meta, enter, esc, tab, space, backspace, delete, up, down, left, right, home, end, page_up, page_down, f1-f12, or any single character.",
    Type.Object({ keys: Type.Array(Type.String()), duration: Type.Number({ description: "Duration in seconds." }) }),
    async ({ keys, duration }, signal) => {
      const normalized = keys.map(normalizeKey);
      for (const key of normalized) await sendCommand("key_down", { key }, signal);
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, duration * 1000);
        signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
      });
      for (const key of [...normalized].reverse()) await sendCommand("key_up", { key }, signal);
      return textResult(`Held ${normalized.join("+")} for ${duration}s`);
    });

  register(pi, "mouse_move", "On a desktop, move the mouse cursor to specified coordinates.",
    Type.Object({ coordinate: Coordinate }),
    async ({ coordinate }, signal) => {
      const { x, y } = await toAbsolute(coordinate, signal);
      await sendCommand("move_cursor", { x, y }, signal);
      return textResult(`Moved cursor to [${coordinate[0]}, ${coordinate[1]}]`);
    });

  register(pi, "click", "On a desktop, perform mouse click at specified coordinates.",
    Type.Object({
      coordinate: Type.Optional(Coordinate),
      button: Type.Optional(Button),
      clicks: Type.Optional(Type.Union([Type.Literal(1), Type.Literal(2), Type.Literal(3)])),
    }),
    async ({ coordinate, button = "left", clicks = 1 }, signal) => {
      const abs = coordinate ? await toAbsolute(coordinate, signal) : null;
      if (clicks === 2 && button === "left") {
        await sendCommand("double_click", abs ? { x: abs.x, y: abs.y } : {}, signal);
      } else if (button === "middle") {
        if (abs) await sendCommand("move_cursor", abs, signal);
        for (let i = 0; i < clicks; i += 1) {
          await sendCommand("mouse_down", { button }, signal);
          await sendCommand("mouse_up", { button }, signal);
        }
      } else {
        const command = button === "right" ? "right_click" : "left_click";
        for (let i = 0; i < clicks; i += 1) await sendCommand(command, abs ?? {}, signal);
      }
      return textResult(`Clicked (${button}, ${clicks}x)${coordinate ? ` at [${coordinate[0]}, ${coordinate[1]}]` : ""}`);
    });

  register(pi, "drag", "On a desktop, drag the mouse from start to end coordinates. Uses mouse_down + move_cursor + mouse_up for reliable cross-platform dragging.",
    Type.Object({ coordinate: Coordinate, start_coordinate: Type.Optional(Coordinate), button: Type.Optional(Button) }),
    async ({ coordinate, start_coordinate, button = "left" }, signal) => {
      const start = start_coordinate ? await toAbsolute(start_coordinate, signal) : null;
      const end = await toAbsolute(coordinate, signal);
      await sendCommand("mouse_down", start ? { ...start, button } : { button }, signal);
      await sendCommand("move_cursor", end, signal);
      await sendCommand("mouse_up", { ...end, button }, signal);
      return textResult(`Dragged (${button}) from ${start_coordinate ? `[${start_coordinate[0]}, ${start_coordinate[1]}]` : "current"} to [${coordinate[0]}, ${coordinate[1]}]`);
    });

  register(pi, "mouse_down", "On a desktop, press the mouse button without releasing.",
    Type.Object({ button: Type.Optional(Button) }),
    async ({ button = "left" }, signal) => {
      await sendCommand("mouse_down", { button }, signal);
      return textResult(`Mouse down: ${button}`);
    });

  register(pi, "mouse_up", "On a desktop, release the mouse button.",
    Type.Object({ button: Type.Optional(Button) }),
    async ({ button = "left" }, signal) => {
      await sendCommand("mouse_up", { button }, signal);
      return textResult(`Mouse up: ${button}`);
    });

  register(pi, "scroll", "On a desktop, scroll in a specified direction by a specified amount.",
    Type.Object({
      direction: Type.Union([Type.Literal("up"), Type.Literal("down"), Type.Literal("left"), Type.Literal("right")]),
      amount: Type.Number({ description: "Number of scroll units." }),
      coordinate: Type.Optional(Coordinate),
    }),
    async ({ direction, amount, coordinate }, signal) => {
      if (coordinate) await sendCommand("move_cursor", await toAbsolute(coordinate, signal), signal);
      await sendCommand("scroll_direction", { direction, clicks: amount }, signal);
      return textResult(`Scrolled ${direction} ${amount}${coordinate ? ` at [${coordinate[0]}, ${coordinate[1]}]` : ""}`);
    });

  register(pi, "wait", "On a desktop, pause execution for a specified duration.",
    Type.Object({ duration: Type.Number({ description: "Time in seconds to wait." }) }),
    async ({ duration }, signal) => {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, duration * 1000);
        signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
      });
      return textResult(`Waited ${duration}s`);
    });

  register(pi, "screenshot", "On a desktop, take a screenshot. Optionally save the image to a path on the VM.",
    Type.Object({ save_path: Type.Optional(Type.String({ description: "Absolute file path on the VM to save the screenshot." })) }),
    async ({ save_path }, signal) => {
      if (save_path !== undefined) {
        const lastSeparator = Math.max(save_path.lastIndexOf("/"), save_path.lastIndexOf("\\"));
        if (lastSeparator <= 0) return { ...textResult(`Error: invalid save_path \"${save_path}\" — must be an absolute path with a parent directory.`), isError: true };
        const parent = save_path.slice(0, lastSeparator);
        const check = await sendCommand("directory_exists", { path: parent }, signal).catch(() => null);
        if (!check?.exists) return { ...textResult(`Error: parent directory \"${parent}\" does not exist on the VM.`), isError: true };
      }
      const result = await sendCommand("screenshot", {}, signal);
      if (!result.image_data) throw new Error("Screenshot returned no image data");
      if (save_path !== undefined) await sendCommand("write_bytes", { path: save_path, content_b64: result.image_data }, signal);
      return {
        content: [
          { type: "text", text: save_path ? `Screenshot captured and saved to ${save_path}` : "Screenshot captured" },
          { type: "image", data: result.image_data, mimeType: "image/png" },
        ],
        details: {},
      };
    });

  register(pi, "cursor_position", "On a desktop, get the current cursor position.", Type.Object({}),
    async (_params, signal) => {
      const result = await sendCommand("get_cursor_position", {}, signal);
      const normalized = await toNormalized(result.position.x, result.position.y, signal);
      return textResult(`Cursor at [${normalized[0]}, ${normalized[1]}]`);
    });
}
