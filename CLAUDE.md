# Notes for agents

## UI regions: canonical names

The Electron interface is divided into the regions below. Use these names,
exactly, in conversation, docs, code identifiers and CSS classes. "Sidebar"
alone is ambiguous: there are three, so always say which.

```
+-----------------------------------------------------------------------+
| meta bar                                                              |
+----------+----------+--------------------------------------+----------+
|          |          |                                      |          |
|          |          |                                      |          |
| project  | details  |                                      | options  |
| sidebar  | sidebar  |             (main area)              | sidebar  |
|          |          |                                      |          |
|          |          |                                      |          |
|          |          |                                      |          |
+----------+----------+--------------------------------------+----------+
| status bar                                                            |
+-----------------------------------------------------------------------+
```

| Name            | Where                                                       |
|-----------------|-------------------------------------------------------------|
| meta bar        | Thin strip across the full width, at the very top.          |
| project sidebar | Leftmost column, between the meta bar and the status bar.   |
| details sidebar | Second column from the left, right of the project sidebar.  |
| options sidebar | Rightmost column, between the meta bar and the status bar.  |
| status bar      | Thin strip across the full width, at the very bottom.       |

Notes:

- The meta bar and status bar span the whole frame; the three sidebars sit
  between them and do not reach the top or bottom edge.
- The large centre region was left unnamed in the schematic. "Main area" here
  is a placeholder, not a canonical name. It is where the window tree of
  `doc/FRONTEND.md` §5.6 goes.
- The status bar is not the modeline. A modeline is the per-window strip
  inside the main area (`doc/FRONTEND.md` §5.6); the status bar is one strip
  for the whole frame.
