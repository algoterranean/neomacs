// The application menu, built from the engine's `menu` event (FRONTEND 8.4, ENGINE 8.6).

import { app, type BrowserWindow, Menu, type MenuItemConstructorOptions } from "electron";
import { accelerator } from "./accelerator.ts";

export interface EngineMenuItem {
  label: string;
  command?: string;
  key?: string;
  enabled?: boolean;
  children?: EngineMenuItem[];
}

/** The menu template for a window.  Choosing an item hands its command to `run`. */
export function template(items: EngineMenuItem[], run: (command: string) => void, mac: boolean): MenuItemConstructorOptions[] {
  const convert = (item: EngineMenuItem): MenuItemConstructorOptions => {
    if (item.children) return { label: item.label, submenu: item.children.map(convert) };
    const shown = accelerator(item.key, mac);
    const command = item.command ?? "";
    return {
      label: item.label,
      enabled: item.enabled !== false && command.length > 0,
      // Shown beside the item; the key itself is the engine's to handle.
      ...(shown ? { accelerator: shown, registerAccelerator: false } : {}),
      click: () => run(command),
    };
  };
  const menus = items.map(convert);
  if (mac) {
    // The items macOS expects of every application.
    menus.unshift({
      label: app.name,
      submenu: [{ role: "about" }, { type: "separator" }, { role: "hide" }, { role: "hideOthers" }, { role: "unhide" }, { type: "separator" }, { role: "quit" }],
    });
  }
  return menus;
}

export function install(window: BrowserWindow | null, items: EngineMenuItem[], run: (command: string) => void): Menu {
  const mac = process.platform === "darwin";
  const menu = Menu.buildFromTemplate(template(items, run, mac));
  // One application menu on macOS; a menu per window elsewhere.
  if (mac || window === null) Menu.setApplicationMenu(menu);
  else window.setMenu(menu);
  return menu;
}
