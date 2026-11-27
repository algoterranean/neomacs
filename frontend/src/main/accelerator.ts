// Engine key notation to the form a native menu shows beside an item (FRONTEND 8.4).

const NAMED: Record<string, string> = {
  "<return>": "Return", "<tab>": "Tab", "<space>": "Space", "<backspace>": "Backspace",
  "<delete>": "Delete", "<escape>": "Escape", "<up>": "Up", "<down>": "Down",
  "<left>": "Left", "<right>": "Right", "<home>": "Home", "<end>": "End",
  "<prior>": "PageUp", "<next>": "PageDown",
};

/**
 * An Electron accelerator for a key in the engine's notation, or null.  Only a
 * single chord can be shown; "C-x C-f" is two.  It is for display: the key
 * itself still goes to the engine as a key.
 */
export function accelerator(key: string | undefined, mac: boolean): string | null {
  if (!key || key.includes(" ")) return null;
  let rest = key;
  const mods: string[] = [];
  const names: Record<string, string> = { C: "Ctrl", M: "Alt", S: "Shift", s: mac ? "Cmd" : "Super" };
  while (rest.length >= 3 && rest[1] === "-" && rest[0] !== undefined && rest[0] in names) {
    mods.push(names[rest[0]] as string);
    rest = rest.slice(2);
  }
  let base: string | null = null;
  if (rest.length === 1) base = /[a-z]/.test(rest) ? rest.toUpperCase() : /[A-Z]/.test(rest) ? rest : /[0-9,./;'[\]\\=`-]/.test(rest) ? rest : null;
  else if (/^<f\d{1,2}>$/.test(rest)) base = rest.slice(1, -1).toUpperCase();
  else base = NAMED[rest] ?? null;
  if (base === null) return null;
  if (rest.length === 1 && /[A-Z]/.test(rest) && !mods.includes("Shift")) mods.push("Shift");
  return [...mods, base].join("+");
}
