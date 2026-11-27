// Turning a keyboard event into the engine's key notation (FRONTEND 8.1, ENGINE 4.7).

export interface KeyEventLike {
  key: string;
  code: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
}

export interface KeyOptions {
  mac: boolean;
  /** On macOS, treat Option as Meta instead of letting it type characters. */
  optionIsMeta: boolean;
}

const NAMED: Record<string, string> = {
  Enter: "<return>",
  Tab: "<tab>",
  Backspace: "<backspace>",
  Delete: "<delete>",
  Escape: "<escape>",
  ArrowLeft: "<left>",
  ArrowRight: "<right>",
  ArrowUp: "<up>",
  ArrowDown: "<down>",
  Home: "<home>",
  End: "<end>",
  PageUp: "<prior>",
  PageDown: "<next>",
  Insert: "<insert>",
  " ": "<space>",
};

const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "AltGraph", "Fn", "Hyper", "Super", "OS"]);

// What each physical key types unshifted and shifted on a US layout.  Used when
// a modifier stops the platform from saying which character the key stands for.
const CODE_CHARS: Record<string, [string, string]> = {
  Minus: ["-", "_"], Equal: ["=", "+"], BracketLeft: ["[", "{"], BracketRight: ["]", "}"],
  Backslash: ["\\", "|"], Semicolon: [";", ":"], Quote: ["'", '"'], Comma: [",", "<"],
  Period: [".", ">"], Slash: ["/", "?"], Backquote: ["`", "~"], Space: [" ", " "],
  Digit1: ["1", "!"], Digit2: ["2", "@"], Digit3: ["3", "#"], Digit4: ["4", "$"], Digit5: ["5", "%"],
  Digit6: ["6", "^"], Digit7: ["7", "&"], Digit8: ["8", "*"], Digit9: ["9", "("], Digit0: ["0", ")"],
}; // prettier-ignore

function fromCode(code: string, shift: boolean): string | null {
  if (code.startsWith("Key") && code.length === 4) {
    const letter = code.slice(3).toLowerCase();
    return shift ? letter.toUpperCase() : letter;
  }
  const pair = CODE_CHARS[code];
  return pair ? pair[shift ? 1 : 0] : null;
}

/**
 * The key in engine notation, or null if this event is not a key press to send:
 * a bare modifier, or part of an input-method composition.
 */
export function translateKey(event: KeyEventLike, options: KeyOptions): string | null {
  if (event.isComposing || event.keyCode === 229) return null;
  if (MODIFIER_KEYS.has(event.key) || event.key === "Dead" || event.key === "Unidentified") return null;

  const meta = event.altKey && (!options.mac || options.optionIsMeta);
  let prefix = (event.ctrlKey ? "C-" : "") + (meta ? "M-" : "");
  const sup = event.metaKey ? "s-" : "";

  const named = NAMED[event.key] ?? (/^F\d{1,2}$/.test(event.key) ? `<${event.key.toLowerCase()}>` : null);
  if (named !== null) {
    // Shift is kept for named keys: S-<left> is not <left>.
    return prefix + (event.shiftKey ? "S-" : "") + sup + named;
  }

  let base: string | null = null;
  if (event.ctrlKey || event.altKey || event.metaKey) {
    // A modifier is held, so `key` may be a control character or, with Option on
    // macOS, some other character entirely.  Go by the physical key instead.
    base = fromCode(event.code, event.shiftKey);
  }
  if (base === null) {
    // One visible character (possibly several UTF-16 units).
    base = [...event.key].length === 1 ? event.key : null;
  }
  if (base === null) return null;
  if (base === " ") base = "<space>";
  if (event.altKey && !meta) prefix = event.ctrlKey ? "C-" : ""; // Option typed a character
  // For a printable key Shift is already in the character: A, not S-a.
  return prefix + sup + base;
}
